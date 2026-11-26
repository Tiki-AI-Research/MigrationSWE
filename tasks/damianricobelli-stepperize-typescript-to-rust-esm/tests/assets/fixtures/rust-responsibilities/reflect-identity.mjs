
import { readFileSync } from 'node:fs';
const echo = new WebAssembly.Instance(new WebAssembly.Module(readFileSync(new URL('./echo.wasm', import.meta.url)))).exports.echo;
const index = (steps, id) => { for (let i=0; i<steps.length; i++) if (Reflect.get(steps[i], "id") === id) return i; return -1; };
const status = (i, current) => ['active', 'previous', 'upcoming'][i === current ? 0 : i >= 0 && i < current ? 1 : 2];
export function createStepMap(steps) {
  return echo({ steps, ids: echo(steps.map(s => Reflect.get(s, "id"))),
    get: id => echo(steps[index(steps,id)]), at: i => echo(steps[i]),
    indexOf: id => echo(index(steps,id)), has: id => echo(index(steps,id)>=0),
    first: () => echo(steps[0]), last: () => echo(steps[steps.length-1]),
    next: id => { const i=index(steps,id); return echo(i>=0 ? steps[i+1] : undefined); },
    prev: id => { const i=index(steps,id); return echo(i>0 ? steps[i-1] : undefined); },
    neighbors: id => { const i=index(steps,id); return echo({prev:i>0?steps[i-1]:undefined,next:i>=0?steps[i+1]:undefined}); },
  });
}
export function getInitialStepIndex(steps,id) { const i=index(steps,id); return echo(i<0?0:i); }
export function parseStep(steps,id) { return echo(index(steps,id)>=0?id:undefined); }
export function getInitialData(data) { return echo({...data}); }
export function getStepStatus(steps,current,id) { return echo(status(index(steps,id),current)); }
export function getStepStatuses(steps,current) { return echo(Object.fromEntries(steps.map((s,i)=>[Reflect.get(s, "id"),status(i,current)]))); }
export function matchStep(steps,id,handlers) {
  const i=index(steps,id); if(i<0) throw new Error('not found');
  if(!Reflect.get(handlers, id)) throw new Error('No match handler'); return echo(Reflect.get(handlers, id)(steps[i]));
}
export async function validateStep(steps,id,value) {
  const i=index(steps,id); if(i<0) throw new Error('not found');
  const schema=Reflect.get(steps[i], "schema"); if(!schema) return echo({success:true,data:value});
  const result=await schema['~standard'].validate(value);
  return echo(Reflect.get(result, "issues")?{success:false,issues:Reflect.get(result, "issues")}:{success:true,data:result.value});
}
