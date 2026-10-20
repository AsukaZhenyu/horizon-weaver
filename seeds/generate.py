"""An editable seed program, not a mandatory runtime workflow."""
import json


def main(task, api):
    instructions = api.request('values.put', value='''Generate a Python component that solves the supplied task.
Return a JSON object with one key, source, containing Python code defining main(task, api).
The return value must be JSON data. Python standard library imports are available.
Host operations use api.request(operation, **arguments). Available operations:
values.put(value, parents=[]), values.get(ref), values.search(query, offset=0);
views.create(), views.load(view, key, ref, role="user"), views.retain(view, key, calls=None), views.evict(view, key);
model.invoke(view) returns request, reply, continuationView, or error;
executables.install(definition) accepts abi="horizon/python-v0", source=<text ref>, bindings={}, grants=[];
call(target=<component ref or binding name>, input=<JSON>).
Use only information in the task and observations you obtain. There is no environment execution operation in this initial prototype.''')
    task_ref = api.request('values.put', value=task)
    view = api.request('views.create')
    view = api.request('views.load', view=view, key='protocol', ref=instructions, role='system')
    view = api.request('views.load', view=view, key='task', ref=task_ref)
    reply = api.request('model.invoke', view=view)
    if 'error' in reply:
        raise RuntimeError(reply['error'])
    response = api.request('values.get', ref=reply['reply'])
    proposal = json.loads(response['content'])
    source = api.request('values.put', value=proposal['source'])
    component = api.request('executables.install', definition={
        'abi': 'horizon/python-v0', 'source': source, 'bindings': {}, 'grants': ['model', 'install']})
    return api.request('call', target=component, input=task)
