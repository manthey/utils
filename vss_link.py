import argparse
import os
import pprint
import pywintypes
import sys
import win32com.client


def vss_create(drive):
    wmi = win32com.client.GetObject('winmgmts:\\\\.\\root\\cimv2:Win32_ShadowCopy')
    createmethod = wmi.Methods_('Create')
    createparams = createmethod.InParameters
    createparams.Properties_[1].value = (
        '%s:\\' % (drive.split(':')[0]) if len(drive) == 1 or
        (':' in drive and drive.index(':') == 1) else drive)
    try:
        results = wmi.ExecMethod_('Create', createparams)
    except pywintypes.com_error:
        sys.stderr.write('Failed.  Was this run without administrator privileges?\n')
        raise
    id = results.Properties_[1].Value
    return id


def vss_delete(id):
    wcd = win32com.client.Dispatch('WbemScripting.SWbemLocator')
    wmi = wcd.ConnectServer('.', 'root\cimv2')
    obj = wmi.ExecQuery('SELECT * FROM Win32_ShadowCopy WHERE ID="%s"' % id)
    obj[0].Delete_()


def vss_list():
    wcd = win32com.client.Dispatch('WbemScripting.SWbemLocator')
    wmi = wcd.ConnectServer('.', 'root\cimv2')
    obj = wmi.ExecQuery('SELECT * FROM Win32_ShadowCopy')
    results = []
    try:
        for o in obj:
            result = {}
            for prop in list(o.Properties_):
                key = prop.Name
                if getattr(o, key) is not None:
                    result[key] = getattr(o, key)
            results.append(result)
    except pywintypes.com_error:
        pass
    return results


if __name__ == '__main__':   # noqa
    parser = argparse.ArgumentParser(
        description='Create or remove Volume Shadow Copy links.')
    parser.add_argument('linkpath', help='Path to create or remove a VSS link')
    parser.add_argument('drive', help='Drive letter to make a VSS link', nargs='?')
    parser.add_argument('--verbose', '-v', action='count', default=0)
    args = parser.parse_args()
    if args.verbose >= 2:
        print('Parsed arguments: %r' % args)
    existing = vss_list()
    if args.verbose >= 1:
        pprint.pprint(existing)
    # Enable to manually prune existing VSS
    if False:
        for info in existing[1:]:
            print(info['ID'])
            vss_delete(info['ID'])
    # remove existing
    try:
        info = open(args.linkpath + '.info').read()
        if info.startswith('VSS '):
            id = info.split('VSS ')[1]
            if args.verbose >= 1:
                print('Unlinking %s' % id)
            vss_delete(id)
            os.unlink(args.linkpath + '.info')
            os.unlink(args.linkpath)
    except Exception as e:
        if args.verbose >= 2:
            raise
        if args.verbose >= 1 and not args.drive:
            print('Failed to unlink: %s' % repr(e))
    if args.drive:
        id = vss_create(args.drive)
        existing = vss_list()
        info = next(vss for vss in existing if vss['ID'] == id)
        try:
            os.symlink(info['DeviceObject'] + '\\', args.linkpath, target_is_directory=True)
            open(args.linkpath + '.info', 'w').write('VSS %s' % id)
            if args.verbose >= 1:
                print('Linked %s' % id)
        except Exception:
            vss_delete(id)
            raise
