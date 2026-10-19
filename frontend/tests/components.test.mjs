import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse, compileScript, compileTemplate, compileStyle } from '@vue/compiler-sfc'

const root = fileURLToPath(new URL('../src', import.meta.url))
function vueFiles(directory) {
    return readdirSync(directory, { withFileTypes: true }).flatMap((entry) =>
        entry.isDirectory()
            ? vueFiles(join(directory, entry.name))
            : entry.name.endsWith('.vue')
              ? [join(directory, entry.name)]
              : [],
    )
}
test('every Vue page compiles and its displayed copy remains intact UTF-8', () => {
    for (const filename of vueFiles(root)) {
        const source = readFileSync(filename, 'utf8')
        const { descriptor, errors } = parse(source, { filename })
        assert.deepEqual(errors, [], filename)
        assert.ok(!/[\uFFFD]|\?{3,}/u.test(descriptor.template?.content || ''), `Corrupted UI text in ${filename}`)
        const script = descriptor.script || descriptor.scriptSetup ? compileScript(descriptor, { id: filename }) : null
        const template = compileTemplate({
            filename,
            id: filename,
            source: descriptor.template?.content || '',
            compilerOptions: { bindingMetadata: script?.bindings || {} },
        })
        assert.deepEqual(template.errors, [], filename)
        for (const style of descriptor.styles)
            assert.deepEqual(
                compileStyle({ filename, id: 'data-v-qa', source: style.content, scoped: style.scoped }).errors,
                [],
                filename,
            )
    }
})
