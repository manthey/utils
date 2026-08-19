import json
import os

sessions = '~/.pi/agent/sessions'
for root, dirs, files in os.walk(os.path.expanduser(sessions)):
    dirs.sort()
    for file in sorted(files):
        data = []
        try:
            for line in open(os.path.join(root, file)).readlines():
                try:
                    record = json.loads(line)
                    msg = record['message']
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
        for entry in data:
            print(entry)
        print()
