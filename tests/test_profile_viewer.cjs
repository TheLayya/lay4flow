// Run with: node tests/test_profile_viewer.cjs
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.join(__dirname,'..');
const viewerCode=fs.readFileSync(path.join(root,'static/js/profile_browser.js'),'utf8');
const viewerHtml=fs.readFileSync(path.join(root,'static/profile-browser.html'),'utf8');

function harness(){
    const elements=new Map(),events=new Map(),calls=[],intervals=new Map(),toasts=[],captures=new Map();
    const element=id=>{
        if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',value:'',hidden:true,style:{},dataset:{},disabled:false,attributes:{},focusCount:0,
            focus(){this.focusCount++;},setAttribute(name,value){this.attributes[name]=value;},contains:target=>target===element(id)||target?.parent===element(id),
            classList:{add(){},remove(){},toggle(){}},addEventListener:(type,fn)=>events.set(id+':'+type,fn)});
        return elements.get(id);
    };
    const location={origin:'https://example.test',protocol:'https:',pathname:'/api/profile-browser/4',href:'/api/profile-browser/4'};
    const popup={closed:false,opener:{},document:{title:'',body:{}},location:{},close(){this.closed=true;}};
    let response={status:200,ok:true,json:async()=>({success:true,viewer_url:'/api/profile-browser/4'})};
    let popupBlocked=false;
    let clipboard=async()=>'';
    let clipboardReads=0;
    const context=vm.createContext({console,URL,Date,Set,Promise,location,
        document:{getElementById:element,addEventListener:(type,fn,capture)=>{events.set('document:'+type,fn);captures.set(type,capture);},createElement:()=>({style:{}}),body:{appendChild:message=>toasts.push(message.textContent)}},
        navigator:{clipboard:{readText(){clipboardReads++;return clipboard();}}},
        window:{addEventListener:(type,fn)=>events.set(type,fn),open(){calls.push('open');return popupBlocked?null:popup;},close(){calls.push('window.close');}},
        setTimeout(){},clearInterval:id=>intervals.delete(id),setInterval:fn=>{intervals.set(1,fn);return 1;},
        fetch:async(url,options={})=>{calls.push({url,options});return typeof response==='function'?response(url,options):response;}
    });
    // The exported viewer actions execute unchanged; RFB connection is covered by the import contract below.
    vm.runInContext(viewerCode.replace(/\nconnect\(\);\s*$/,''),context);
    const ui=vm.runInContext('({connect,closeBrowser,pollStatus,refreshOpenCredentials,focusNativeBrowser,sendPasteText,pasteFromClipboard,disconnect,setBusy})',context);
    return {ui,popup,location,calls,toasts,elements,events,context,intervals,captures,
        respond:value=>response=value,blockPopup:()=>popupBlocked=true,
        clipboard:value=>clipboard=typeof value==='function'?value:async()=>value,clipboardReads:()=>clipboardReads};
}

function connectRfb(page){
    page.context.keyCalls=[];page.context.focusCount=0;page.context.rfbDisconnects=0;
    vm.runInContext('rfb={sendKey(...args){keyCalls.push(args);},focus(){focusCount++;},disconnect(){rfbDisconnects++;}};connected=true;updatePasteControls();',page.context);
    return page.context.keyCalls;
}

function inputEvent(target,extra={}){
    return {target,prevented:false,stopped:false,preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;},...extra};
}

async function flushPromises(){await Promise.resolve();await Promise.resolve();}

async function main(){
    const native=harness();
    native.respond({status:200,ok:true,json:async()=>({success:true,status:'ready',presentation:'native'})});
    await native.ui.connect();
    assert.equal(native.calls.length,1,'Native presentation reads state without importing noVNC or opening WebSocket');
    assert.equal(native.calls[0].url,'/api/tokens/4/browser');
    assert.match(native.elements.get('screen').innerHTML,/本地 Cloak 窗口/);
    assert.equal(native.elements.get('pasteText').hidden,true);
    assert.equal(native.elements.get('focusNativeBrowser').hidden,false);
    await native.ui.focusNativeBrowser();
    assert.equal(native.calls.at(-1).url,'/api/tokens/4/browser/focus');
    assert.equal(native.calls.at(-1).options.method,'POST');
    assert.match(native.elements.get('status').textContent,/已显示本地 Cloak 窗口/);

    const viewer=harness();let releaseClose;
    viewer.respond((url,options)=>options.method==='DELETE'?new Promise(resolve=>releaseClose=resolve):
        {status:200,ok:true,json:async()=>({success:true,message:'刷新成功'})});
    const refreshing=viewer.ui.closeBrowser(true);
    assert.equal(viewer.calls.length,1);assert.equal(viewer.calls[0].options.method,'DELETE');
    assert.equal(viewer.elements.get('refreshCredentials').disabled,true);
    await viewer.ui.closeBrowser(true);assert.equal(viewer.calls.length,1);
    releaseClose({status:200,ok:true,json:async()=>({success:true,status:'closed'})});
    await refreshing;
    assert.equal(viewer.calls[1].url,'/api/tokens/4/refresh-at');
    assert.equal(viewer.calls[1].options.method,'POST');
    assert.equal(viewer.elements.get('status').textContent,'刷新成功');
    viewer.events.get('pagehide')();assert.equal(viewer.calls.length,2,'already closed browser is not closed again on unload');

    const keepOpen=harness();let releaseRefresh;
    keepOpen.context.rfbDisconnects=0;
    vm.runInContext('rfb={disconnect(){rfbDisconnects++;}};connected=true;pollTimer=setInterval(pollStatus,5000);',keepOpen.context);
    keepOpen.respond(()=>new Promise(resolve=>releaseRefresh=resolve));
    const inPlaceRefreshing=keepOpen.events.get('refreshOpenCredentials:click')();
    assert.equal(keepOpen.calls.length,1,'keep-open refresh makes one request');
    assert.equal(keepOpen.calls[0].url,'/api/tokens/4/browser/refresh');
    assert.equal(keepOpen.calls[0].options.method,'POST');
    assert.equal(keepOpen.calls[0].options.credentials,'same-origin');
    for(const id of ['refreshCredentials','refreshOpenCredentials','closeBrowser'])assert.equal(keepOpen.elements.get(id).disabled,true);
    await keepOpen.ui.refreshOpenCredentials();await keepOpen.ui.closeBrowser();await keepOpen.ui.pollStatus();
    assert.equal(keepOpen.calls.length,1,'busy refresh prevents duplicate refresh, close, and poll requests');
    releaseRefresh({status:200,ok:true,json:async()=>({success:true,message:'AT刷新成功',token:{at_expires:'2030-01-02T03:04:05+00:00'}})});
    await inPlaceRefreshing;
    assert.equal(keepOpen.elements.get('status').textContent,'AT刷新成功；AT 到期：2030-01-02T03:04:05+00:00');
    assert.equal(keepOpen.elements.get('refreshOpenCredentials').disabled,false);
    assert.ok(!keepOpen.calls.some(call=>call.options?.method==='DELETE'),'keep-open refresh never closes the browser');
    assert.ok(!keepOpen.calls.includes('window.close'),'keep-open refresh never closes the window');
    assert.equal(keepOpen.context.rfbDisconnects,0,'RFB remains connected after refreshing');
    assert.equal(keepOpen.intervals.size,1,'status polling remains active after refreshing');
    const resultStatus=keepOpen.elements.get('status').textContent;
    keepOpen.respond({status:200,ok:true,json:async()=>({status:'ready'})});
    await keepOpen.ui.pollStatus();
    assert.equal(keepOpen.elements.get('status').textContent,resultStatus,'ready polling preserves refresh result');
    keepOpen.events.get('pagehide')();
    assert.equal(keepOpen.calls.at(-1).options.method,'DELETE','unload cleanup remains separate from keep-open refresh');

    const topLevelExpires=harness();
    topLevelExpires.respond({status:200,ok:true,json:async()=>({success:true,at_expires:'2030-02-03T04:05:06Z'})});
    await topLevelExpires.ui.refreshOpenCredentials();
    assert.ok(topLevelExpires.elements.get('status').textContent.includes('2030-02-03T04:05:06Z'));

    const closeFailure=harness();closeFailure.respond({status:409,ok:false,json:async()=>({detail:'暂时无法关闭'})});
    await closeFailure.ui.closeBrowser(true);assert.equal(closeFailure.calls.length,1,'refresh cannot run before a successful close');
    assert.equal(closeFailure.elements.get('status').dataset.error,'true');
    closeFailure.events.get('pagehide')();assert.equal(closeFailure.calls.at(-1).options.keepalive,true);
    assert.equal(closeFailure.calls.at(-1).options.credentials,'same-origin');

    const authFailure=harness();authFailure.respond({status:401,ok:false,json:async()=>({})});
    await authFailure.ui.pollStatus();assert.ok(authFailure.elements.get('status').textContent.includes('重新登录'));
    assert.equal(authFailure.location.href,'/api/profile-browser/4','authentication failures do not navigate and abandon the viewer');

    const backendFailure=harness();backendFailure.respond({status:200,ok:true,json:async()=>({status:'error'})});
    await backendFailure.ui.pollStatus();
    assert.equal(backendFailure.elements.get('status').textContent,'浏览器关闭失败，请重启调试容器后重试');
    assert.equal(backendFailure.elements.get('status').dataset.error,'true');
    await backendFailure.ui.pollStatus();assert.equal(backendFailure.calls.length,1,'backend error stops polling');
    backendFailure.respond({status:409,ok:false,json:async()=>({detail:'关闭失败'})});
    await backendFailure.ui.closeBrowser(true);
    assert.equal(backendFailure.calls.at(-1).options.method,'DELETE','backend error cannot be treated as a successful close');
    assert.ok(!backendFailure.calls.some(call=>call.url.endsWith('/refresh-at')));

    const pasted=harness(),keys=connectRfb(pasted);
    const modifierKeys=[0xffe3,0xffe4,0xffeb,0xffec,0xffe1,0xffe2,0xffe9,0xffea];
    const characterKeys=()=>keys.filter(call=>call.length===2).map(call=>call[0]);
    assert.equal(pasted.ui.sendPasteText('Aé中😀\r\n\t\x7f\x80'),true);
    assert.deepEqual(keys.slice(0,8).map(call=>Array.from(call)),modifierKeys.map(key=>[key,null,false]),'remote modifiers are released before typing');
    assert.deepEqual(characterKeys(),[65,233,0x01004e2d,0x0101f600],'ASCII, Latin-1, Unicode and astral characters map to X11 keysyms');
    assert.ok(keys.every(call=>call[1]===null),'text uses keysyms without a physical keycode');
    assert.equal(pasted.calls.length,0,'pasting does not send text to an HTTP endpoint');
    assert.equal(pasted.elements.get('pasteInput').value,'');
    assert.equal(pasted.elements.get('pastePanel').hidden,true);
    for(const state of ['connected=false','busy=true','closed=true','failed=true']){
        vm.runInContext('connected=true;busy=false;closed=false;failed=false;'+state,pasted.context);
        const count=keys.length;
        assert.equal(pasted.ui.sendPasteText('SYNTHETIC-PASSWORD'),false);
        assert.equal(keys.length,count,'inactive viewer does not send keys: '+state);
    }

    const panel=harness(),panelKeys=connectRfb(panel);
    panel.events.get('pasteText:click')();
    const panelInput=panel.elements.get('pasteInput');
    assert.equal(panel.elements.get('pastePanel').hidden,false);
    assert.equal(panel.elements.get('pasteText').attributes['aria-expanded'],'true');
    assert.equal(panelInput.focusCount,1);
    assert.equal(panel.elements.get('pasteSend').disabled,true);
    panelInput.value='SYNTHETIC-PASSWORD!';panel.events.get('pasteInput:input')();
    assert.equal(panel.elements.get('pasteSend').disabled,false);
    panel.events.get('pasteSend:click')();
    assert.equal(panelKeys.filter(call=>call.length===2).map(call=>String.fromCodePoint(call[0])).join(''),'SYNTHETIC-PASSWORD!');
    assert.equal(panelInput.value,'');assert.equal(panel.elements.get('pastePanel').hidden,true);
    assert.equal(panel.elements.get('pasteText').attributes['aria-expanded'],'false');
    assert.equal(panel.calls.length,0);assert.equal(panel.toasts.length,0);
    assert.equal(panel.elements.get('status').textContent,'','sensitive text is not shown in status');
    for(const dismiss of ['cancel','escape','busy','disconnect','pagehide']){
        vm.runInContext('connected=true;busy=false;closed=false;failed=false;',panel.context);
        panel.events.get('pasteText:click')();panelInput.value='SYNTHETIC-PASSWORD!';
        if(dismiss==='cancel')panel.events.get('pasteCancel:click')();
        if(dismiss==='escape'){
            const event=inputEvent(panelInput,{key:'Escape'});panel.events.get('pasteInput:keydown')(event);assert.equal(event.prevented,true);
        }
        if(dismiss==='busy')panel.ui.setBusy(true);
        if(dismiss==='disconnect')panel.ui.disconnect();
        if(dismiss==='pagehide')panel.events.get('pagehide')();
        assert.equal(panelInput.value,'','panel input is erased on '+dismiss);
        assert.equal(panel.elements.get('pastePanel').hidden,true);
    }
    assert.ok(!JSON.stringify(panel.calls).includes('SYNTHETIC-PASSWORD!'),'unload cleanup does not carry pasted content');

    const direct=harness(),directKeys=connectRfb(direct),screenElement=direct.elements.get('screen');
    const canvas={parent:screenElement};
    let clipboardType;
    const pasteEvent=inputEvent(canvas,{clipboardData:{getData(type){clipboardType=type;return 'Direct文本';}}});
    direct.events.get('document:paste')(pasteEvent);
    assert.equal(direct.captures.get('paste'),true);assert.equal(clipboardType,'text/plain');
    assert.equal(pasteEvent.prevented,true);assert.equal(pasteEvent.stopped,true);
    assert.deepEqual(directKeys.filter(call=>call.length===2).map(call=>call[0]),[68,105,114,101,99,116,0x01006587,0x0100672c]);
    assert.equal(direct.clipboardReads(),0,'direct paste uses the event clipboard without permission prompts');
    const nonScreen=inputEvent(direct.elements.get('pasteInput'),{clipboardData:{getData(){throw new Error('must stay native');}}});
    direct.events.get('document:paste')(nonScreen);assert.equal(nonScreen.prevented,false);
    vm.runInContext('connected=false;',direct.context);
    const disconnectedEvent=inputEvent(canvas,{clipboardData:{getData(){throw new Error('not connected');}}});
    direct.events.get('document:paste')(disconnectedEvent);assert.equal(disconnectedEvent.prevented,false);
    assert.equal(direct.calls.length,0);

    const shortcut=harness(),shortcutKeys=connectRfb(shortcut),shortcutCanvas={parent:shortcut.elements.get('screen')};
    shortcut.clipboard('Shortcut文本');
    const keydown=inputEvent(shortcutCanvas,{key:'v',ctrlKey:true,metaKey:false,repeat:false});
    shortcut.events.get('document:keydown')(keydown);
    await flushPromises();
    assert.equal(keydown.prevented,true);assert.equal(keydown.stopped,true);
    assert.equal(shortcut.clipboardReads(),1);assert.equal(shortcutKeys.filter(call=>call.length===2).length,10);
    const keyup=inputEvent(shortcutCanvas,{key:'v'});shortcut.events.get('document:keyup')(keyup);
    assert.equal(keyup.prevented,true);assert.equal(keyup.stopped,true);
    const unrelatedKeyup=inputEvent(shortcutCanvas,{key:'v'});shortcut.events.get('document:keyup')(unrelatedKeyup);
    assert.equal(unrelatedKeyup.prevented,false,'only the intercepted shortcut keyup is swallowed');
    shortcut.clipboard(async()=>{throw new Error('clipboard permission denied');});
    const commandPaste=inputEvent(shortcutCanvas,{key:'V',ctrlKey:false,metaKey:true,repeat:false});
    shortcut.events.get('document:keydown')(commandPaste);await flushPromises();
    assert.equal(commandPaste.prevented,true);assert.equal(shortcut.elements.get('pastePanel').hidden,false,'denied clipboard opens masked manual paste');
    assert.equal(shortcut.elements.get('pasteInput').value,'');
    let releaseClipboard;shortcut.clipboard(()=>new Promise(resolve=>releaseClipboard=resolve));
    const reading=shortcut.ui.pasteFromClipboard();await shortcut.ui.pasteFromClipboard();
    assert.equal(shortcut.clipboardReads(),3,'parallel shortcut requests read clipboard once');
    const sentBeforeDisconnect=shortcutKeys.length;shortcut.ui.disconnect();releaseClipboard('SYNTHETIC-PASSWORD!');await reading;
    assert.equal(shortcutKeys.length,sentBeforeDisconnect,'clipboard result is discarded if the viewer disconnected while reading');
    assert.equal(shortcut.elements.get('pastePanel').hidden,true);assert.equal(shortcut.calls.length,0);

    assert.ok(viewerCode.includes("import('/api/profile-browser-assets/core/rfb.js')"));
    assert.ok(viewerCode.includes('`/api/profile-browser/${tokenId}/ws`'));
    assert.ok(!/localStorage|sessionStorage|indexedDB|clipboardPasteFrom/.test(viewerCode),'pasted text is not persisted or shared with the remote clipboard');
    assert.match(viewerHtml,/<input\b[^>]*id="pasteInput"[^>]*type="password"[^>]*autocomplete="off"/);
    console.log('PASS: Profile viewer cookie auth, credential refresh, sensitive-text paste, and unload cleanup');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
