import {describe, expect, it} from 'vitest'
import {readFileSync} from 'node:fs'
import {OpenAiService} from '../../../src/ai/OpenAiService'
import {applyEditorToolProposal, executeEditorFunctionTool} from '../../../src/ai/editor-tools'
import type {AiEditorToolProposal} from '../../../src/ai/types'
import {
    assertAgentTransport,
    assistantMessage,
    createEditor,
    createFakeClient,
    functionCall,
    makeContext,
    response,
    textStream,
} from '../../../fixtures/batches/BATCH-F2P-0002/fake-model'

function answer(text: string) {
    return (_body: any) => response([assistantMessage(text)])
}

function deferred<T = void>() {
    let resolve!: (value: T) => void
    let reject!: (reason?: unknown) => void
    const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
    return {promise, resolve, reject}
}

function toolContext(overrides: Record<string, unknown> = {}) {
    const editor = createEditor({text: 'abc'})
    return makeContext({text: 'abc', selectedText: '', selection: {from: 1, to: 3}, documentVersion: 7, editor, ...overrides})
}

describe('BATCH-F2P-0002 target probes', () => {
    it('F2P-025 model response options reach the target Agents transport and proposals keep their shape', async () => {
        const client = createFakeClient(answer('x'))
        const service = new OpenAiService({
            client,
            provider: 'openai',
            model: 'gpt-4o-mini',
            response: {temperature: 0.1, top_p: 0.8, max_output_tokens: 64, parallel_tool_calls: false, store: false},
        })
        await service.generate({prompt: 'x'}, makeContext())
        const body = client.calls[0].body
        assertAgentTransport(client.calls[0])
        expect(body.temperature).toBe(0.1)
        expect(body.top_p).toBe(0.8)
        expect(body.max_output_tokens).toBe(64)
        expect(body.parallel_tool_calls).toBe(false)
        expect(body.store).toBe(false)

        const execution = executeEditorFunctionTool('replace_selection', '{"text":"x"}', toolContext())
        const proposal = execution.proposal!
        const status: AiEditorToolProposal['status'] = proposal.status
        expect(Object.keys(proposal)).toEqual(expect.arrayContaining(['id', 'tool', 'title', 'description', 'arguments', 'documentVersion', 'status']))
        expect(['pending', 'applied', 'discarded']).toContain(status)
        expect(status).toBe('pending')
    })

    it('F2P-027 target runner text, stream, and tool orchestration preserve call ids', async () => {
        const textClient = createFakeClient(answer('agent'))
        const textService = new OpenAiService({client: textClient, provider: 'openai', model: 'gpt-4o-mini'})
        await expect(textService.generate({prompt: 'hi'}, makeContext())).resolves.toMatchObject({text: 'agent'})
        assertAgentTransport(textClient.calls[0])

        const streamClient = createFakeClient(() => textStream(['agent']))
        const streamService = new OpenAiService({client: streamClient, provider: 'openai', model: 'gpt-4o-mini'})
        const chunks: string[] = []
        await expect(streamService.generate({prompt: 'hi', stream: true, onChunk: (chunk) => chunks.push(chunk)}, makeContext()))
            .resolves.toMatchObject({text: 'agent'})
        assertAgentTransport(streamClient.calls[0])
        expect(chunks).toEqual(['agent'])

        const toolClient = createFakeClient((_body, _options, index) => index === 1
            ? response([functionCall('search_document', {query: 'a'}, 'call_1')])
            : response([assistantMessage('agent')]))
        const toolService = new OpenAiService({client: toolClient, provider: 'openai', model: 'gpt-4o-mini'})
        const result = await toolService.generate({prompt: 'hi', editorTools: true}, toolContext())
        expect(result.text).toBe('agent')
        assertAgentTransport(toolClient.calls[1])
        const secondInput = toolClient.calls[1].body.input
        const output = secondInput.find((item: any) => item.type === 'function_call_output')
        expect(output.call_id).toBe('call_1')
        expect(JSON.parse(output.output)).toMatchObject({ok: true, query: 'a'})
    })

    it('F2P-029 replace-selection proposals and provider model precedence use the target provider', async () => {
        const execution = executeEditorFunctionTool('replace_selection', '{"text":"x"}', toolContext())
        expect(execution.output).toMatchObject({ok: true, status: 'pending_user_approval'})
        expect(execution.output.proposal_id).toBe(execution.proposal!.id)
        expect(execution.proposal).toMatchObject({status: 'pending', documentVersion: 7})

        const endpointService = new OpenAiService({
            provider: {id: 'q', name: 'Q', baseURL: 'https://example.test/v1', model: 'm1'},
            apiKey: 'sk-test',
            dangerouslyAllowBrowser: true,
        })
        expect(endpointService.client.baseURL).toBe('https://example.test/v1')

        const providerClient = createFakeClient(answer('ok'))
        const configured = new OpenAiService({
            client: providerClient,
            provider: {id: 'q', name: 'Q', baseURL: 'https://example.test/v1', model: 'm1'},
        })
        await configured.generate({prompt: 'hi'}, makeContext())
        expect(providerClient.calls[0].body.model).toBe('m1')
        assertAgentTransport(providerClient.calls[0])

        const client = createFakeClient(answer('ok'))
        const overridden = new OpenAiService({
            client,
            provider: {id: 'q', name: 'Q', baseURL: 'https://example.test/v1', model: 'm1'},
            model: 'm2',
        })
        await overridden.generate({prompt: 'hi'}, makeContext())
        expect(client.calls[0].body.model).toBe('m2')
        assertAgentTransport(client.calls[0])

        expect(() => new OpenAiService({provider: {id: 'q', name: 'Q', model: '   '}})).toThrow('OpenAiService requires a model')
    })

    it('F2P-033 a caller-supplied client is the target transport client', async () => {
        const client = createFakeClient(answer('supplied'))
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        expect(service.client).toBe(client)
        await expect(service.generate({prompt: 'hi'}, makeContext())).resolves.toMatchObject({text: 'supplied'})
        expect(client.calls).toHaveLength(1)
        assertAgentTransport(client.calls[0])
    })

    it('F2P-037 the runtime entry executes through the Agents SDK rather than the Responses loop', async () => {
        const client = createFakeClient(answer('entry'))
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        await expect(service.generate({prompt: 'hi'}, makeContext())).resolves.toMatchObject({text: 'entry'})
        assertAgentTransport(client.calls[0])
        expect(client.calls).toHaveLength(1)
        expect(client.calls[0].body.stream).toBe(false)
        expect(client.calls[0].body.tools ?? []).toEqual([])
    })

    it('F2P-028 default input composition is preserved while the client is driven by the target runner', async () => {
        const client = createFakeClient((_body, options) => {
            if (!String(options?.headers?.['User-Agent'] ?? '').includes('Agents/JavaScript')) {
                throw new Error('direct client.responses.create is forbidden')
            }
            return response([assistantMessage('ok')])
        })
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        const context = makeContext({text: 'doc', selectedText: '', selection: {from: 1, to: 1}})
        await service.generate({prompt: 'p', history: [{role: 'user', content: 'u'}, {role: 'assistant', content: 'a'}]}, context)
        expect(client.calls[0].body.input[0].content).toBe('User: u\nAssistant: a\nUser: p\n\nDocument:\ndoc')
        assertAgentTransport(client.calls[0])

        await service.generate({prompt: 'p'}, makeContext({text: 'doc', selectedText: 'sel'}))
        expect(client.calls[1].body.input[0].content).toBe('p\n\nSelected text:\nsel')

        await service.generate({prompt: 'p', scope: 'none'}, makeContext({text: 'doc', selectedText: 'sel'}))
        expect(client.calls[2].body.input[0].content).toBe('p')

        const customized = new OpenAiService({
            client,
            provider: 'openai',
            model: 'gpt-4o-mini',
            buildInput: () => 'custom input',
        })
        await customized.generate({prompt: 'p'}, context)
        expect(client.calls[3].body.input[0].content).toBe('custom input')
        assertAgentTransport(client.calls[3])
    })

    it('F2P-032 aborted streams reject without partial text and non-stream tool rounds accumulate text', async () => {
        const abort = new AbortController()
        const client = createFakeClient((_body, options) => {
            const signal: AbortSignal | undefined = options?.signal
            return new ReadableStream({
                start(controller) {
                    controller.enqueue({type: 'response.created', response: response([])})
                    controller.enqueue({type: 'response.output_text.delta', delta: 'partial'})
                    signal?.addEventListener('abort', () => {
                        try { controller.error(signal.reason) } catch { /* already closed */ }
                    }, {once: true})
                },
                cancel() { /* target cancellation */ },
            })
        })
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        const pending = service.generate({prompt: 'x', stream: true, signal: abort.signal}, makeContext())
        const reason = new Error('stop')
        setTimeout(() => abort.abort(reason), 5)
        await expect(pending).rejects.toBe(reason)

        const rounds = createFakeClient((_body, _options, index) => index === 1
            ? response([assistantMessage('a'), functionCall('search_document', {query: 'a'}, 'call_1')])
            : response([assistantMessage('b')]))
        const roundService = new OpenAiService({client: rounds, provider: 'openai', model: 'gpt-4o-mini'})
        await expect(roundService.generate({prompt: 'x', editorTools: true}, toolContext())).resolves.toMatchObject({text: 'ab'})
        assertAgentTransport(rounds.calls[1])
    })

    it('F2P-043 generate results, empty prompts, and separate instructions follow the target contract', async () => {
        const client = createFakeClient(answer('hello'))
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        const result = await service.generate({prompt: 'hello'}, makeContext())
        expect(result.text).toBe('hello')
        expect(result).toHaveProperty('raw')
        await expect(service.generate({prompt: '   '}, makeContext())).rejects.toThrow('AI prompt cannot be empty')
        expect(client.calls).toHaveLength(1)

        await service.generate({prompt: 'p', instructions: 'Be terse'}, makeContext({text: 'doc'}))
        expect(client.calls[1].body.instructions).toBe('Be terse')
        expect(client.calls[1].body.input[0].content).toBe('p\n\nDocument:\ndoc')
        assertAgentTransport(client.calls[1])
    })

    it('F2P-026 call identifiers are continuous and the eight-round limit is enforced', async () => {
        // A final response without calls ends the loop even if it has no text.
        // Exercise empty output and refusals, including after text, in both execution modes.
        const refusal = {
            type: 'message', id: 'refusal_1', role: 'assistant', status: 'completed',
            content: [{type: 'refusal', refusal: 'Unable to answer'}],
        }
        for (const {output: finalOutput, text: finalText} of [
            {output: [], text: ''},
            {output: [refusal], text: ''},
            {output: [assistantMessage('after'), refusal], text: 'after'},
        ]) {
            for (const stream of [false, true]) {
                const terminalClient = createFakeClient((_body, _options, index) => {
                    const output = index === 1
                        ? [assistantMessage('before'), functionCall('search_document', {query: 'a'}, 'terminal_call')]
                        : finalOutput
                    return stream
                        ? textStream(index === 1 ? ['before'] : (finalText ? [finalText] : []), output)
                        : response(output, index === 1 ? 'tool_round' : 'terminal_round')
                })
                const terminalService = new OpenAiService({client: terminalClient, provider: 'openai', model: 'gpt-4o-mini'})
                const terminalChunks: string[] = []
                const terminal = await terminalService.generate({
                    prompt: 'x', editorTools: true, stream,
                    ...(stream ? {onChunk: (chunk: string) => terminalChunks.push(chunk)} : {}),
                }, toolContext())
                expect(terminal.text).toBe(`before${finalText}`)
                expect(terminal.raw).toBeDefined()
                expect(terminalClient.calls).toHaveLength(2)
                expect(terminalClient.calls[1].body.input.find((item: any) => item.type === 'function_call_output').call_id)
                    .toBe('terminal_call')
                if (stream) expect(terminal.text).toBe(terminalChunks.join(''))
                for (const call of terminalClient.calls) assertAgentTransport(call)
            }
        }

        const client = createFakeClient((_body, _options, index) => index === 1
            ? response([functionCall('search_document', {query: 'a'}, 'call_1')])
            : response([assistantMessage('done')]))
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        await expect(service.generate({prompt: 'x', editorTools: true}, toolContext())).resolves.toMatchObject({text: 'done'})
        const output = client.calls[1].body.input.find((item: any) => item.type === 'function_call_output')
        expect(output.call_id).toBe('call_1')
        assertAgentTransport(client.calls[1])

        const looping = createFakeClient((_body, _options, index) =>
            response([functionCall('search_document', {query: 'a'}, `call_${index}`)]))
        const loopingService = new OpenAiService({client: looping, provider: 'openai', model: 'gpt-4o-mini'})
        await expect(loopingService.generate({prompt: 'x', editorTools: true}, toolContext()))
            .rejects.toThrow('AI exceeded the maximum editor tool call rounds')
        expect(looping.calls).toHaveLength(8)
    })

    it('F2P-030 proposal-producing tools and multi-round text accumulation agree', async () => {
        const execution = executeEditorFunctionTool('replace_selection', '{"text":"x"}', toolContext())
        expect(execution.proposal).toMatchObject({tool: 'replace_selection', status: 'pending', documentVersion: 7})
        expect(execution.output).toMatchObject({ok: true, status: 'pending_user_approval'})

        const client = createFakeClient((_body, _options, index) => index === 1
            ? response([assistantMessage('a'), functionCall('search_document', {query: 'a'}, 'call_1')])
            : response([assistantMessage('b')]))
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        await expect(service.generate({prompt: 'x', editorTools: true}, toolContext())).resolves.toMatchObject({text: 'ab'})
        assertAgentTransport(client.calls[1])

        // The SDK's finalOutput is only the last text part of the last
        // assistant message. The public result must retain all output text.
        const multipart = assistantMessage('a', 'multipart')
        multipart.content.push({type: 'output_text', text: 'b', annotations: []})
        const refusal = {
            type: 'message', id: 'refusal', role: 'assistant', status: 'completed',
            content: [{type: 'refusal', refusal: 'Unable to continue'}],
        }
        for (const editorTools of [false, true]) {
            for (const output of [
                [multipart],
                [assistantMessage('a', 'first'), assistantMessage('b', 'last')],
                [assistantMessage('ab'), refusal],
                [assistantMessage('ab', 'first'), assistantMessage('', 'last')],
            ]) {
                const finalClient = createFakeClient(() => response(output, 'final_text'))
                const finalService = new OpenAiService({client: finalClient, provider: 'openai', model: 'gpt-4o-mini'})
                const result = await finalService.generate({prompt: 'x', editorTools}, toolContext())
                expect(result.text).toBe('ab')
                if (editorTools) expect(result.raw).toMatchObject({id: 'final_text'})
                expect(finalClient.calls).toHaveLength(1)
                assertAgentTransport(finalClient.calls[0])
            }
        }
    })

    it('F2P-031 apply results, the target dependency, and supplied-client transport are preserved', async () => {
        const editor = createEditor({text: 'abc'})
        const proposal = {id: 'p1', tool: 'replace_selection', title: 'T', description: 'D', arguments: {_selection_from: 1, _selection_to: 3, text: 'x'}, documentVersion: 1, status: 'pending'}
        const applied = applyEditorToolProposal(editor, 1, proposal as any)
        expect(applied).toMatchObject({ok: true, message: 'Applied to the document.'})
        const rejected = applyEditorToolProposal(editor, 1, {...proposal, tool: 'search_document'} as any)
        expect(rejected.ok).toBe(false)
        expect(typeof rejected.message).toBe('string')

        const packageJson = JSON.parse(readFileSync('package.json', 'utf8'))
        expect(packageJson.dependencies['@openai/agents']).toBe('0.1.0')
        const client = createFakeClient(answer('ok'))
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        await service.generate({prompt: 'hi'}, makeContext())
        assertAgentTransport(client.calls[0])
    })

    it('F2P-034 proposals keep their shape and the raw provider response is returned', async () => {
        const execution = executeEditorFunctionTool('replace_selection', '{"text":"x"}', toolContext())
        expect(execution.proposal).toMatchObject({tool: 'replace_selection', status: 'pending', documentVersion: 7})
        const client = createFakeClient(() => response([assistantMessage('alpha')], 'resp_1'))
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        const result = await service.generate({prompt: 'write'}, makeContext())
        expect(result).toMatchObject({text: 'alpha', raw: {id: 'resp_1'}})
        assertAgentTransport(client.calls[0])
    })

    it('F2P-035 request-time context snapshots stay isolated while the target runner executes', async () => {
        const started = deferred()
        const release = deferred()
        const client = createFakeClient(async (body, options) => {
            started.resolve()
            await release.promise
            return response([assistantMessage('ok')])
        })
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        const context = makeContext({text: 'original', selectedText: 'ori'})
        const pending = service.generate({prompt: 'p'}, context)
        await started.promise
        context.text = 'mutated'
        context.selectedText = 'mutated'
        release.resolve()
        await pending
        expect(client.calls[0].body.input[0].content).toBe('p\n\nSelected text:\nori')
        assertAgentTransport(client.calls[0])
    })

    it('F2P-036 the eight-round limit is enforced and raw responses remain available', async () => {
        const looping = createFakeClient((_body, _options, index) =>
            response([functionCall('search_document', {query: 'a'}, `call_${index}`)], `resp_${index}`))
        const service = new OpenAiService({client: looping, provider: 'openai', model: 'gpt-4o-mini'})
        await expect(service.generate({prompt: 'x', editorTools: true}, toolContext()))
            .rejects.toThrow('AI exceeded the maximum editor tool call rounds')
        expect(looping.calls).toHaveLength(8)

        const client = createFakeClient(() => response([assistantMessage('alpha')], 'resp_1'))
        const rawService = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        const result = await rawService.generate({prompt: 'write'}, makeContext())
        expect(result.raw).toMatchObject({id: 'resp_1'})
        assertAgentTransport(client.calls[0])
    })

    it('F2P-038 model precedence and incomplete streamed tool rounds fail with the target error', async () => {
        const client = createFakeClient(answer('ok'))
        const service = new OpenAiService({client, provider: {id: 'p', name: 'P', model: 'provider-model'}, model: 'config-model'})
        await service.generate({prompt: 'x'}, makeContext())
        expect(client.calls[0].body.model).toBe('config-model')
        assertAgentTransport(client.calls[0])
        const providerModel = new OpenAiService({client, provider: {id: 'p', name: 'P', model: 'provider-model'}})
        await providerModel.generate({prompt: 'x'}, makeContext())
        expect(client.calls[1].body.model).toBe('provider-model')
        expect(() => new OpenAiService({provider: {id: 'p', name: 'P', model: '   '}})).toThrow('OpenAiService requires a model')

        const incomplete = createFakeClient(() => (async function* () {
            yield {type: 'response.created', response: response([])}
            yield {type: 'response.output_text.delta', delta: 'partial'}
        })())
        const incompleteService = new OpenAiService({client: incomplete, provider: 'openai', model: 'gpt-4o-mini'})
        await expect(incompleteService.generate({prompt: 'x', stream: true, editorTools: true}, toolContext()))
            .rejects.toThrow('AI response stream ended before completion')
    })

    it('F2P-039 apply results are stable while the target turn limit is enforced', async () => {
        const editor = createEditor({text: 'abc'})
        const proposal = {id: 'p1', tool: 'replace_selection', title: 'T', description: 'D', arguments: {_selection_from: 1, _selection_to: 3, text: 'x'}, documentVersion: 1, status: 'pending'}
        expect(applyEditorToolProposal(editor, 1, proposal as any)).toMatchObject({ok: true, message: 'Applied to the document.'})
        expect(applyEditorToolProposal(editor, 1, {...proposal, tool: 'get_editor_context'} as any).ok).toBe(false)
        const looping = createFakeClient((_body, _options, index) =>
            response([functionCall('search_document', {query: 'a'}, `call_${index}`)]))
        const service = new OpenAiService({client: looping, provider: 'openai', model: 'gpt-4o-mini'})
        await expect(service.generate({prompt: 'x', editorTools: true}, toolContext()))
            .rejects.toThrow('AI exceeded the maximum editor tool call rounds')
        expect(looping.calls).toHaveLength(8)
        assertAgentTransport(looping.calls[7])
    })

    it('F2P-040 incomplete streamed tool rounds reject and thrown tools continue generation', async () => {
        const incomplete = createFakeClient(() => (async function* () {
            yield {type: 'response.created', response: response([])}
            yield {type: 'response.output_text.delta', delta: 'partial'}
        })())
        const incompleteService = new OpenAiService({client: incomplete, provider: 'openai', model: 'gpt-4o-mini'})
        await expect(incompleteService.generate({prompt: 'x', stream: true, editorTools: true}, toolContext()))
            .rejects.toThrow('AI response stream ended before completion')

        const throwingContext = toolContext()
        throwingContext.editor.state.doc.descendants = () => { throw new Error('boom') }
        const client = createFakeClient((_body, _options, index) => index === 1
            ? response([functionCall('search_document', {query: 'a'}, 'call_1')])
            : response([assistantMessage('recovered')]))
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        await expect(service.generate({prompt: 'x', editorTools: true}, throwingContext)).resolves.toMatchObject({text: 'recovered'})
        const output = client.calls[1].body.input.find((item: any) => item.type === 'function_call_output')
        expect(JSON.parse(output.output)).toEqual({ok: false, error: 'boom'})
        assertAgentTransport(client.calls[1])
    })

    it('F2P-041 thrown tool output continues the loop and raw responses are returned', async () => {
        const throwingContext = toolContext()
        throwingContext.editor.state.doc.descendants = () => { throw new Error('bad args') }
        const client = createFakeClient((_body, _options, index) => index === 1
            ? response([functionCall('search_document', {query: 'a'}, 'call_1')])
            : response([assistantMessage('alpha')], 'resp_1'))
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        const result = await service.generate({prompt: 'write', editorTools: true}, throwingContext)
        expect(result).toMatchObject({text: 'alpha', raw: {id: 'resp_1'}})
        const output = client.calls[1].body.input.find((item: any) => item.type === 'function_call_output')
        expect(JSON.parse(output.output)).toEqual({ok: false, error: 'bad args'})
        assertAgentTransport(client.calls[1])

        // Invalid JSON must reach the same local error handling as other
        // invalid arguments, even when the SDK parses arguments before invoke.
        for (const rawArguments of ['{', '', '[]']) {
            let expectedError = ''
            try {
                executeEditorFunctionTool('search_document', rawArguments, toolContext())
            } catch (error) {
                expectedError = error instanceof Error ? error.message : 'Tool execution failed'
            }
            expect(expectedError).not.toBe('')
            for (const stream of [false, true]) {
                const recoveryClient = createFakeClient((_body, _options, index) => {
                    const items = index === 1
                        ? [functionCall('search_document', rawArguments, 'bad_arguments')]
                        : [assistantMessage('recovered')]
                    return stream ? textStream([], items) : response(items, 'recovered_response')
                })
                const recoveryService = new OpenAiService({client: recoveryClient, provider: 'openai', model: 'gpt-4o-mini'})
                const recovered = await recoveryService.generate({prompt: 'x', editorTools: true, stream}, toolContext())
                expect(recovered.text).toBe(stream ? '' : 'recovered')
                expect(recovered.toolProposals).toEqual([])
                expect(recoveryClient.calls).toHaveLength(2)
                const answered = recoveryClient.calls[1].body.input.find((item: any) => item.type === 'function_call_output')
                expect(answered.call_id).toBe('bad_arguments')
                expect(JSON.parse(answered.output)).toEqual({ok: false, error: expectedError})
                assertAgentTransport(recoveryClient.calls[1])
            }
        }
    })

    it('F2P-042 the request carrying every field streams only text deltas', async () => {
        const client = createFakeClient(() => textStream(['a', 'b', 'c'], [assistantMessage('abc')], [
            {type: 'response.in_progress', response: response([])},
            {type: 'response.content_part.added', part: {type: 'output_text', text: ''}},
            {type: 'response.output_item.added', item: {type: 'message'}},
        ]))
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        const chunks: string[] = []
        const request = {
            prompt: 'x',
            instructions: 'Be brief',
            scope: 'document' as const,
            metadata: {requestId: 'r1'},
            history: [{role: 'user' as const, content: 'u'}],
            stream: true,
            onChunk: (chunk: string) => chunks.push(chunk),
            editorTools: false,
            signal: new AbortController().signal,
        }
        await expect(service.generate(request, makeContext({text: 'doc'}))).resolves.toMatchObject({text: 'abc'})
        expect(chunks).toEqual(['a', 'b', 'c'])
        assertAgentTransport(client.calls[0])

        // A completed response can carry text without any text-delta event.
        // Streaming text must still equal the deltas delivered to onChunk,
        // including when the callback alone enables streaming or tools are on.
        for (const editorTools of [false, true]) {
            for (const stream of [undefined, true]) {
                const noDeltaClient = createFakeClient(() => textStream([], [assistantMessage('completed-only')]))
                const noDeltaService = new OpenAiService({client: noDeltaClient, provider: 'openai', model: 'gpt-4o-mini'})
                const noDeltaChunks: string[] = []
                const result = await noDeltaService.generate({
                    prompt: 'x', editorTools, stream, onChunk: (chunk) => noDeltaChunks.push(chunk),
                }, toolContext())
                expect(noDeltaChunks).toEqual([])
                expect(result.text).toBe(noDeltaChunks.join(''))
                expect(noDeltaClient.calls).toHaveLength(1)
                expect(noDeltaClient.calls[0].body.stream).toBe(true)
                assertAgentTransport(noDeltaClient.calls[0])
            }
        }
    })

    it('F2P-044 a full request type is accepted and aborting a pending target stream rejects with its reason', async () => {
        // A supplied transport may finish despite cancellation. Check the
        // service boundary as well as cancellation of an active stream.
        for (const [editorTools, returnsToolCall] of [[false, false], [true, false], [true, true]]) {
            const cancellation = new AbortController()
            const reason = new Error('cancel non-streaming generation')
            const cancelledClient = createFakeClient(() => {
                cancellation.abort(reason)
                return response(returnsToolCall
                    ? [functionCall('search_document', {query: 'a'}, 'cancelled_call')]
                    : [assistantMessage('must not be returned')])
            })
            const cancelledService = new OpenAiService({client: cancelledClient, provider: 'openai', model: 'gpt-4o-mini'})
            await expect(cancelledService.generate({prompt: 'x', editorTools, signal: cancellation.signal}, toolContext()))
                .rejects.toBe(reason)
            expect(cancelledClient.calls).toHaveLength(1)
            assertAgentTransport(cancelledClient.calls[0])
        }

        const abort = new AbortController()
        const client = createFakeClient((_body, options) => new ReadableStream({
            start(controller) {
                controller.enqueue({type: 'response.created', response: response([])})
                controller.enqueue({type: 'response.output_text.delta', delta: 'partial'})
                options?.signal?.addEventListener('abort', () => {
                    try { controller.error(options.signal.reason) } catch { /* already closed */ }
                }, {once: true})
            },
            cancel() { /* target cancellation */ },
        }))
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        const reason = new Error('stop')
        const request = {
            prompt: 'x',
            instructions: 'Be brief',
            scope: 'document' as const,
            metadata: {requestId: 'r1'},
            history: [{role: 'user' as const, content: 'u'}],
            stream: true,
            onChunk: () => undefined,
            editorTools: false,
            signal: abort.signal,
        }
        const pending = service.generate(request, makeContext())
        setTimeout(() => abort.abort(reason), 5)
        await expect(pending).rejects.toBe(reason)
        expect(client.calls).toHaveLength(1)
        assertAgentTransport(client.calls[0])
    })

    it('F2P-045 the OpenAiService contract uses the target dependency and supplied client', async () => {
        const client = createFakeClient(answer('ok'))
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        expect(service.client).toBe(client)
        const result = service.generate({prompt: 'hi'}, makeContext())
        expect(result).toBeInstanceOf(Promise)
        await expect(result).resolves.toMatchObject({text: 'ok'})
        const packageJson = JSON.parse(readFileSync('package.json', 'utf8'))
        expect(packageJson.dependencies['@openai/agents']).toBe('0.1.0')
        assertAgentTransport(client.calls[0])
    })

    it('F2P-046 instructions stay model-level while the target runner handles the request', async () => {
        const client = createFakeClient(answer('ok'))
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        await service.generate({prompt: 'p', instructions: 'Be brief'}, makeContext({text: 'doc'}))
        expect(client.calls[0].body.instructions).toBe('Be brief')
        expect(client.calls[0].body.input[0].content).toBe('p\n\nDocument:\ndoc')
        expect(client.calls[0].body.input[0].content).not.toContain('Be brief')
        assertAgentTransport(client.calls[0])
    })

    it('F2P-047 thrown tool output continues generation and incomplete streams reject', async () => {
        const throwingContext = toolContext()
        throwingContext.editor.state.doc.descendants = () => { throw new Error('bad args') }
        const client = createFakeClient((_body, _options, index) => index === 1
            ? response([functionCall('search_document', {query: 'a'}, 'call_1')])
            : response([assistantMessage('done')]))
        const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
        await expect(service.generate({prompt: 'x', editorTools: true}, throwingContext)).resolves.toMatchObject({text: 'done'})
        expect(JSON.parse(client.calls[1].body.input.find((item: any) => item.type === 'function_call_output').output))
            .toEqual({ok: false, error: 'bad args'})
        assertAgentTransport(client.calls[1])

        const incomplete = createFakeClient(() => (async function* () {
            yield {type: 'response.created', response: response([])}
            yield {type: 'response.output_text.delta', delta: 'partial'}
        })())
        const incompleteService = new OpenAiService({client: incomplete, provider: 'openai', model: 'gpt-4o-mini'})
        await expect(incompleteService.generate({prompt: 'x', stream: true, editorTools: true}, toolContext()))
            .rejects.toThrow('AI response stream ended before completion')
    })

    it('F2P-048 context snapshots remain stable and response options reach the target transport', async () => {
        const started = deferred()
        const release = deferred()
        const client = createFakeClient(async () => {
            started.resolve()
            await release.promise
            return response([assistantMessage('ok')])
        })
        const service = new OpenAiService({
            client,
            provider: 'openai',
            model: 'gpt-4o-mini',
            response: {temperature: 0.1, top_p: 0.8, max_output_tokens: 64, parallel_tool_calls: false, store: false},
        })
        const context = makeContext({text: 'original', selectedText: 'ori'})
        const pending = service.generate({prompt: 'p'}, context)
        await started.promise
        context.text = 'mutated'
        context.selectedText = 'mutated'
        release.resolve()
        await pending
        expect(client.calls[0].body.input[0].content).toBe('p\n\nSelected text:\nori')
        expect(client.calls[0].body).toMatchObject({temperature: 0.1, top_p: 0.8, max_output_tokens: 64, parallel_tool_calls: false, store: false})
        assertAgentTransport(client.calls[0])
    })
})
