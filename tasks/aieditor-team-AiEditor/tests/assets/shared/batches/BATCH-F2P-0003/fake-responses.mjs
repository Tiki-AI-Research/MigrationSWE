let sequence = 0

function nextId(prefix) {
  sequence += 1
  return `${prefix}_${sequence}`
}

export function messageItem(text, id = nextId('msg')) {
  return {
    type: 'message',
    id,
    role: 'assistant',
    status: 'completed',
    content: [{type: 'output_text', text, annotations: []}],
  }
}

export function functionCallItem({id = nextId('fc'), callId = nextId('call'), name, args = '{}'}) {
  return {
    type: 'function_call',
    id,
    call_id: callId,
    name,
    arguments: typeof args === 'string' ? args : JSON.stringify(args),
    status: 'completed',
  }
}

export function responseObject(id, output) {
  const response = {
    id,
    object: 'response',
    created_at: 0,
    status: 'completed',
    model: 'gpt-4o-mini',
    output,
    usage: {
      input_tokens: 1,
      output_tokens: 1,
      total_tokens: 2,
      input_tokens_details: {cached_tokens: 0},
      output_tokens_details: {reasoning_tokens: 0},
    },
  }
  Object.defineProperty(response, 'output_text', {
    enumerable: false,
    get() {
      return output
        .filter((item) => item.type === 'message')
        .flatMap((item) => item.content ?? [])
        .filter((content) => content.type === 'output_text')
        .map((content) => content.text)
        .join('')
    },
  })
  return response
}

export function streamEvents(id, deltas, output = [messageItem(deltas.join(''))]) {
  const events = [{type: 'response.created', response: {id, object: 'response', status: 'in_progress', output: []}}]
  for (const delta of deltas) {
    events.push({type: 'response.output_text.delta', item_id: 'msg', output_index: 0, content_index: 0, delta})
  }
  events.push({type: 'response.completed', response: responseObject(id, output)})
  return events
}

export async function* asyncEvents(events) {
  for (const event of events) {
    await Promise.resolve()
    yield event
  }
}

export function makeFakeClient(script) {
  const calls = []
  let index = 0
  const client = {
    responses: {
      create: async (body, options) => {
        const call = {body, options, stack: new Error().stack ?? ''}
        calls.push(call)
        const plan = typeof script === 'function' ? script(body, index) : script[Math.min(index, script.length - 1)]
        index += 1
        if (!plan) throw new Error('fake responses script has no step')
        if (plan.error) throw plan.error
        if (plan.events) return asyncEvents(plan.events)
        if (plan.response) return plan.response
        throw new Error('fake responses script returned an invalid step')
      },
    },
  }
  return {client, calls}
}
