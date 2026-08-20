import json
import os

totalcount = 0
sessions = '~/.pi/agent/sessions'
for root, dirs, files in os.walk(os.path.expanduser(sessions)):  # noqa
    dirs.sort()
    for file in sorted(files):
        data = []
        count = {'user': 0, 'assistant': 0}
        usage = {'input': 0, 'output': 0, 'max': 0}
        try:
            for line in open(os.path.join(root, file)).readlines():
                try:
                    record = json.loads(line)
                    msg = record['message']
                    if record['type'] == 'message':
                        count[msg['role']] = count.get(msg['role'], 0) + 1
                        if 'usage' in msg:
                            usage['input'] += msg['usage']['input']
                            usage['output'] += msg['usage']['output']
                            usage['max'] = max(usage['max'], msg['usage']['totalTokens'])
                    if record['type'] != 'message' or msg['role'] != 'user':
                        continue
                    for content in msg['content']:
                        text = content['text']
                        if text not in data:
                            data.append(text)
                except Exception:
                    pass
        except Exception:
            pass
        if not data:
            continue
        print(f'## {os.path.join(root, file)}')
        print('- Calls: ' + ', '.join([f'{k}: {count[k]}' for k in count]))
        print('- Usage: ' + ', '.join([f'{k}: {usage[k]}' for k in usage]))
        for entry in data:
            print(entry)
        print()
        totalcount += 1
print(f'## Summary\n- Sessions: {totalcount}')
