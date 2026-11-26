export type FakeCall = {body: any; options: any}

export function assistantMessage(text: string, id = 'msg_1') {
    return {
        type: 'message',
        id,
        role: 'assistant',
        status: 'completed',
        content: [{type: 'output_text', text, annotations: []}],
    }
}

export function functionCall(name: string, args: unknown, callId = 'call_1', id = 'fc_1') {
    return {
        type: 'function_call',
        id,
        call_id: callId,
        name,
        arguments: typeof args === 'string' ? args : JSON.stringify(args),
        status: 'completed',
    }
}

export function response(output: any[], id = 'resp_1') {
    const text = output
        .filter((item: any) => item.type === 'message')
        .flatMap((item: any) => item.content ?? [])
        .filter((part: any) => part.type === 'output_text')
        .map((part: any) => part.text)
        .join('')
    return {
        id,
        object: 'response',
        created_at: 0,
        status: 'completed',
        model: 'gpt-4o-mini',
        output,
        output_text: text,
        usage: {
            input_tokens: 1,
            output_tokens: 1,
            total_tokens: 2,
            input_tokens_details: {},
            output_tokens_details: {},
        },
    }
}

export function createFakeClient(handler: (body: any, options: any, callIndex: number) => any) {
    const calls: FakeCall[] = []
    const client = {
        calls,
        responses: {
            create: async (body: any, options: any) => {
                calls.push({body, options})
                return await handler(body, options, calls.length)
            },
        },
        chat: {
            completions: {
                create: async () => {
                    throw new Error('target transport must use the Responses API through the Agents SDK')
                },
            },
        },
    }
    return client as any
}

export function assertAgentTransport(call: FakeCall): void {
    const userAgent = String(call.options?.headers?.['User-Agent'] ?? '')
    if (!userAgent.includes('Agents/JavaScript') || !userAgent.includes('0.1.0')) {
        throw new Error(`model request did not execute through @openai/agents 0.1.0 (User-Agent: ${userAgent || 'missing'})`)
    }
}

export function textStream(chunks: string[], output = [assistantMessage(chunks.join(''))], extraEvents: any[] = []) {
    return (async function* () {
        yield {type: 'response.created', response: response([])}
        for (const event of extraEvents) yield event
        for (const delta of chunks) yield {type: 'response.output_text.delta', delta}
        yield {type: 'response.completed', response: response(output)}
    })()
}

export function createEditor(options: {text?: string; size?: number} = {}) {
    const text = options.text ?? 'original'
    const size = options.size ?? text.length + 2
    return {
        state: {
            doc: {
                content: {size},
                textBetween: (from: number, to: number) => text.slice(Math.max(0, from - 1), Math.max(0, to - 1)),
                descendants: (_callback: any) => undefined,
            },
            selection: {
                $from: {parent: {type: {name: 'paragraph'}}, marks: () => []},
                storedMarks: null,
            },
        },
        chain: () => {
            const chain: any = {
                focus: () => chain,
                setTextSelection: () => chain,
                setMark: () => chain,
                wrapIn: () => chain,
                setNode: () => chain,
                setTextAlign: () => chain,
                insertContentAt: () => chain,
                command: (callback: any) => {
                    callback({insertText: () => undefined})
                    return chain
                },
                run: () => true,
            }
            return chain
        },
    } as any
}

export function makeContext(overrides: Record<string, unknown> = {}) {
    const text = typeof overrides.text === 'string' ? overrides.text : 'abc'
    return {
        editor: createEditor({text}),
        html: `<p>${text}</p>`,
        text,
        selectedText: '',
        selection: {from: 1, to: 1},
        documentVersion: 1,
        ...overrides,
    } as any
}

export function makeProposal(tool: string, overrides: Record<string, unknown> = {}) {
    return {
        id: `proposal-${tool}`,
        tool,
        title: 'Title',
        description: 'Description',
        arguments: {},
        documentVersion: 1,
        status: 'pending',
        ...overrides,
    } as any
}
