const match=location.pathname.match(/^\/api\/profile-browser\/([1-9]\d*)$/);
const tokenId=match?.[1];
const viewerId=new URL(location.href,location.origin).searchParams.get('viewer_id');
const viewerQuery=viewerId?`?viewer_id=${encodeURIComponent(viewerId)}`:'';
const statusElement=document.getElementById('status');
const screen=document.getElementById('screen');
const closeButton=document.getElementById('closeBrowser');
const refreshButton=document.getElementById('refreshCredentials');
const refreshOpenButton=document.getElementById('refreshOpenCredentials');
const pasteButton=document.getElementById('pasteText');
const pastePanel=document.getElementById('pastePanel');
const pasteInput=document.getElementById('pasteInput');
const pasteSendButton=document.getElementById('pasteSend');
const pasteCancelButton=document.getElementById('pasteCancel');
const focusNativeButton=document.getElementById('focusNativeBrowser');
let rfb=null,pollTimer=null,busy=false,closed=false,connected=false,failed=false,credentialResult=false,clipboardReading=false,pasteShortcut=false;
let nativePresentation=window.lay4flowDesktop?.runtime==='native';

function showNativeWindow(){
    screen.dataset.presentation='native';
    screen.innerHTML='<p>本地 Cloak 窗口已在桌面打开。请直接在桌面窗口登录或操作；本页可刷新凭据、保存并关闭。</p>';
    pasteButton.hidden=true;focusNativeButton.hidden=false;
    const footer=document.getElementById('profileFooter');
    if(footer)footer.textContent='直接在桌面 Cloak 窗口输入与粘贴；点击“刷新凭据，保持窗口”可保存当前登录态。';
}

function canPaste(){return Boolean(rfb&&connected&&!busy&&!closed&&!failed);}

function updatePasteControls(){
    pasteButton.disabled=!canPaste();
    pasteSendButton.disabled=!canPaste()||!pasteInput.value;
}

function hidePastePanel(refocus=true){
    pasteInput.value='';pastePanel.hidden=true;
    pasteButton.setAttribute('aria-expanded','false');
    updatePasteControls();
    if(refocus&&canPaste())rfb.focus();
}

function showPastePanel(){
    if(!canPaste())return;
    pasteInput.value='';pastePanel.hidden=false;
    pasteButton.setAttribute('aria-expanded','true');
    updatePasteControls();pasteInput.focus();
}

function sendPasteText(text){
    if(!canPaste()||typeof text!=='string')return false;
    // A shortcut may have already sent modifier-down events to the remote desktop.
    for(const keysym of [0xffe3,0xffe4,0xffeb,0xffec,0xffe1,0xffe2,0xffe9,0xffea])rfb.sendKey(keysym,null,false);
    for(const character of text){
        const point=character.codePointAt(0);
        // Paste single-line input only: control characters must not submit or change focus.
        if(point<0x20||(point>=0x7f&&point<=0x9f))continue;
        rfb.sendKey(point<0x100?point:0x01000000|point,null);
    }
    hidePastePanel();
    return true;
}

async function pasteFromClipboard(){
    if(!canPaste()||clipboardReading)return;
    clipboardReading=true;
    try{
        if(!navigator.clipboard?.readText)throw new Error('clipboard');
        const text=await navigator.clipboard.readText();
        if(!sendPasteText(text)&&canPaste())showPastePanel();
    }catch{showPastePanel();}
    finally{clipboardReading=false;}
}

function setStatus(message,error=false){
    statusElement.textContent=message;
    statusElement.dataset.error=String(error);
}

function setBusy(value){
    busy=value;
    closeButton.disabled=value;refreshButton.disabled=value;
    focusNativeButton.disabled=value||closed||failed;
    refreshOpenButton.disabled=value||closed||failed;
    if(value)hidePastePanel(false);
    updatePasteControls();
}

async function request(path,method='GET'){
    const response=await fetch(path+(path.includes('/browser')?viewerQuery:''),{method,credentials:'same-origin'});
    const data=await response.json().catch(()=>({}));
    if(response.status===401)throw new Error('管理登录已过期，请返回管理页重新登录');
    if(!response.ok||data.success===false)throw new Error(data.detail||data.message||'操作失败，请稍后重试');
    return data;
}

function disconnect(){
    connected=false;
    hidePastePanel(false);
    if(pollTimer!==null){clearInterval(pollTimer);pollTimer=null;}
    rfb?.disconnect();
}

async function closeBrowser(refresh=false){
    if(busy)return;
    setBusy(true);credentialResult=false;
    try{
        if(!closed){
            setStatus('正在保存并关闭浏览器…');
            await request(`/api/tokens/${tokenId}/browser`,'DELETE');
            closed=true;disconnect();
        }
        if(refresh){
            setStatus('浏览器已关闭，正在刷新凭据…');
            const result=await request(`/api/tokens/${tokenId}/refresh-at`,'POST');
            setStatus(result.message||(result.queued?'已加入刷新队列，请返回管理页查看结果':'凭据刷新成功'));
        }else{
            setStatus('浏览器已关闭');
            window.close();
            location.href='/manage';
        }
    }catch(error){setStatus(error.message,true);}
    finally{setBusy(false);}
}

async function refreshOpenCredentials(){
    if(busy||closed||failed)return;
    setBusy(true);credentialResult=false;setStatus('正在刷新凭据，浏览器保持打开…');
    try{
        const result=await request(`/api/tokens/${tokenId}/browser/refresh`,'POST');
        const expires=result.token?.at_expires||result.at_expires;
        setStatus((result.message||'凭据刷新成功')+(expires?`；AT 到期：${expires}`:''));
    }catch(error){setStatus(error.message,true);}
    finally{credentialResult=true;setBusy(false);}
}

async function focusNativeBrowser(){
    if(!nativePresentation||busy||closed||failed)return;
    setBusy(true);
    try{
        await request(`/api/tokens/${tokenId}/browser/focus`,'POST');
        setStatus('已显示本地 Cloak 窗口，请直接在桌面窗口操作');
    }catch(error){setStatus(error.message,true);}
    finally{setBusy(false);}
}

async function pollStatus(){
    if(busy||closed||failed)return;
    try{
        const state=await request(`/api/tokens/${tokenId}/browser`);
        if(state.presentation==='native'||nativePresentation){
            nativePresentation=true;connected=false;hidePastePanel(false);
            if(state.status==='error'){failed=true;refreshOpenButton.disabled=true;disconnect();setStatus('本地 Cloak 窗口关闭失败，请重启软件后重试',true);}
            else if(state.status==='closed'){closed=true;refreshOpenButton.disabled=true;disconnect();setStatus('本地 Cloak 窗口已关闭，请返回管理页重新打开');}
            else if(state.status==='closing'&&!credentialResult)setStatus('本地 Cloak 窗口正在关闭…');
            else if(!credentialResult)setStatus('本地 Cloak 窗口已打开，请直接在桌面窗口操作');
            return;
        }
        if(state.status==='error'){
            failed=true;refreshOpenButton.disabled=true;disconnect();setStatus('浏览器关闭失败，请重启调试容器后重试',true);
        }else if(state.status==='closed'){
            closed=true;refreshOpenButton.disabled=true;disconnect();setStatus('浏览器已关闭，请返回管理页重新打开');
        }else if(state.status==='closing'&&!credentialResult){
            setStatus('浏览器正在关闭…');
        }else if(state.status==='starting'&&!credentialResult){
            setStatus('正在启动浏览器…');
        }else if(connected&&!credentialResult){
            setStatus('已连接，可以操作浏览器');
        }
    }catch(error){setStatus(error.message,true);}
}

async function connect(){
    if(!tokenId){setStatus('账号浏览器地址无效',true);closeButton.disabled=true;refreshButton.disabled=true;refreshOpenButton.disabled=true;return;}
    document.getElementById('accountTitle').textContent=`账号 ${tokenId} 的浏览器`;
    try{
        await pollStatus();
        if(nativePresentation){
            showNativeWindow();
            if(!closed&&!failed)pollTimer=setInterval(pollStatus,5000);
            return;
        }
        if(closed||failed)return;
        const {default:RFB}=await import('/api/profile-browser-assets/core/rfb.js');
        const address=new URL(`/api/profile-browser/${tokenId}/ws`,location.origin);
        address.search=viewerQuery;
        address.protocol=location.protocol==='https:'?'wss:':'ws:';
        screen.textContent='';
        rfb=new RFB(screen,address.href);
        rfb.scaleViewport=true;
        rfb.resizeSession=false;
        rfb.viewOnly=false;
        rfb.addEventListener('connect',()=>{if(!closed&&!failed){connected=true;updatePasteControls();if(!busy&&!credentialResult)setStatus('已连接，可以操作浏览器');rfb.focus();}});
        rfb.addEventListener('disconnect',()=>{connected=false;hidePastePanel(false);if(!closed&&!failed)setStatus('画面连接已断开，请返回管理页重新打开',true);});
        rfb.addEventListener('securityfailure',()=>setStatus('浏览器连接认证失败，请返回管理页重试',true));
        await pollStatus();
        if(!closed&&!failed)pollTimer=setInterval(pollStatus,5000);
    }catch(error){setStatus('无法连接浏览器：'+error.message,true);}
}

closeButton.addEventListener('click',()=>closeBrowser());
refreshButton.addEventListener('click',()=>closeBrowser(true));
refreshOpenButton.addEventListener('click',refreshOpenCredentials);
pasteButton.addEventListener('click',showPastePanel);
pasteSendButton.addEventListener('click',()=>sendPasteText(pasteInput.value));
pasteCancelButton.addEventListener('click',()=>hidePastePanel());
focusNativeButton.addEventListener('click',focusNativeBrowser);
pasteInput.addEventListener('input',updatePasteControls);
pasteInput.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();hidePastePanel();}});
document.addEventListener('paste',event=>{
    if(!screen.contains(event.target)||!canPaste())return;
    event.preventDefault();event.stopImmediatePropagation();
    sendPasteText(event.clipboardData?.getData('text/plain')||'');
},true);
document.addEventListener('keydown',event=>{
    if(!screen.contains(event.target)||!(event.ctrlKey||event.metaKey)||event.key.toLowerCase()!=='v'||!canPaste())return;
    event.preventDefault();event.stopImmediatePropagation();pasteShortcut=true;
    if(!event.repeat)pasteFromClipboard();
},true);
document.addEventListener('keyup',event=>{
    if(pasteShortcut&&event.key.toLowerCase()==='v'){
        pasteShortcut=false;event.preventDefault();event.stopImmediatePropagation();
    }
},true);
window.addEventListener('pagehide',()=>{
    disconnect();
    if(tokenId&&!closed)fetch(`/api/tokens/${tokenId}/browser${viewerQuery}`,{method:'DELETE',credentials:'same-origin',keepalive:true}).catch(()=>{});
});
connect();
