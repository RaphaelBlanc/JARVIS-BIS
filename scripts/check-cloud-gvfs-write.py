#!/usr/bin/env python3
"""Exercise native writable Gio/GVfs against disposable storage + gateway.

Requires gateway/cloud Python dependencies, /usr/bin/python3 with Gio,
GVfs WebDAV, and D-Bus. Never uses personal configuration or production data.
Credentials are passed to the helper on stdin, never in its arguments.
"""
import importlib.util,json,os,secrets,subprocess,sys,tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'gateway')]
spec=importlib.util.spec_from_file_location('integration_checks',ROOT/'scripts/check-cloud-noctalia.py')
checks=importlib.util.module_from_spec(spec);spec.loader.exec_module(checks)

HELPER=r'''
import json,sys,time
from gi.repository import Gio,GLib
p=json.load(sys.stdin)
root=Gio.File.new_for_uri(p['url'].replace('http://','dav://')+'/cloud/dav/')
op=Gio.MountOperation();op.set_password_save(Gio.PasswordSave.NEVER)
def auth(o,*args):
    o.set_username(p['device']);o.set_password(p['token']);o.reply(Gio.MountOperationResult.HANDLED)
op.connect('ask-password',auth)
loop=GLib.MainLoop();failure=[]
def done(o,r):
    try:o.mount_enclosing_volume_finish(r)
    except Exception as e:failure.append(e)
    loop.quit()
root.mount_enclosing_volume(Gio.MountMountFlags.NONE,op,None,done);loop.run()
assert not failure,str(failure)
try:
    folder=root.get_child(p['prefix']+' Dossier été #100%?');folder.make_directory(None)
    content=b'GVfs-streamed-content\n'*15000
    file=folder.get_child('Document été.txt')
    ok,etag=file.replace_contents(content,None,False,Gio.FileCreateFlags.NONE,None)
    assert ok and file.load_contents(None)[1]==content
    etag=file.query_info('etag::value',Gio.FileQueryInfoFlags.NONE,None).get_etag()
    assert etag, 'GVfs did not expose an ETag'
    ok,updated=file.replace_contents(b'replaced',etag,False,Gio.FileCreateFlags.NONE,None)
    assert ok and file.load_contents(None)[1]==b'replaced'
    try:file.replace_contents(b'stale',etag,False,Gio.FileCreateFlags.NONE,None)
    except GLib.Error:pass
    else:raise AssertionError('stale native overwrite accepted')
    local=Gio.File.new_for_path(p['local'])
    copied=folder.get_child('copié depuis PC.txt')
    local.copy(copied,Gio.FileCopyFlags.NONE,None,None,None)
    assert copied.load_contents(None)[1]==b'local source'
    renamed=file.set_display_name('renommé #100%?.txt',None)
    destination=root.get_child(p['prefix']+' Destination');destination.make_directory(None)
    moved=destination.get_child('déplacé.txt')
    renamed.move(moved,Gio.FileCopyFlags.NONE,None,None,None)
    assert moved.load_contents(None)[1]==b'replaced'
    copied.delete(None);folder.delete(None)
    moved.delete(None);destination.delete(None)
    print('PASS: real Gio/GVfs MKCOL, streamed PUT, verified read, ETag replacement/stale refusal, local copy, rename, move and delete')
finally:
    mount=root.find_enclosing_mount(None)
    def unmounted(o,r):
        try:o.unmount_with_operation_finish(r)
        except Exception as e:failure.append(e)
        loop.quit()
    mount.unmount_with_operation(Gio.MountUnmountFlags.NONE,None,None,unmounted);loop.run()
assert not failure,str(failure)
'''


def main(tmp):
    from fastapi import FastAPI
    from cloud.app import create_app
    from cloud.storage import Settings
    from cloud_proxy import router
    key=secrets.token_hex(32)
    os.environ.update(DEPLOY_MODE='server',CLOUD_ENABLED='1',CLOUD_DAV_WRITE='1',CLOUD_INTERNAL_KEY=key)
    for name in ('GNOME_KEYRING_CONTROL','GNOME_KEYRING_PID','SSH_AUTH_SOCK'):
        os.environ.pop(name,None)
    for name in ('HOME','XDG_CONFIG_HOME','XDG_DATA_HOME','XDG_STATE_HOME','XDG_CACHE_HOME','XDG_RUNTIME_DIR','TMPDIR'):
        path=tmp/name;path.mkdir(mode=0o700);os.environ[name]=str(path)
    os.environ['GNOME_KEYRING_CONTROL']=str(tmp/'XDG_RUNTIME_DIR/keyring')
    Path(os.environ['GNOME_KEYRING_CONTROL']).mkdir(mode=0o700)
    os.environ['GIO_USE_PORTALS']='0'
    checks.run('dbus-update-activation-environment','HOME','XDG_CONFIG_HOME','XDG_DATA_HOME','XDG_STATE_HOME','XDG_CACHE_HOME','XDG_RUNTIME_DIR','TMPDIR','GIO_USE_PORTALS','GNOME_KEYRING_CONTROL')
    daemon=subprocess.Popen(['/usr/libexec/gvfsd','--no-fuse'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    app=create_app(Settings(tmp/'storage',quota=16*1024**2,max_file=2*1024**2,chunk_size=65536,dav_write=True),key)
    device=app.state.store.associate('Gio native check')
    journal=[]
    class Log:
        async def __call__(self,scope,receive,send):
            async def sent(message):
                if message['type']=='http.response.start':journal.append((scope['method'],scope['path'],message['status'],dict(scope['headers']).get(b'if-match'),dict(scope['headers']).get(b'if')))
                await send(message)
            await app(scope,receive,sent)
    try:
        with checks.serve(Log()) as internal:
            os.environ['CLOUD_INTERNAL_URL']=internal
            gateway=FastAPI();gateway.include_router(router(lambda:None))
            with checks.serve(gateway) as public:
                source=tmp/'source.txt';source.write_bytes(b'local source')
                payload=dict(url=public,device=device['device_id'],token=device['token'],local=str(source),prefix='gvfs-'+secrets.token_hex(5))
                result=subprocess.run(['/usr/bin/python3','-c',HELPER],input=json.dumps(payload),capture_output=True,text=True,timeout=60)
                if result.returncode:
                    print(result.stdout);print(result.stderr);print('HTTP journal:',journal)
                    return result.returncode
                print(result.stdout)
                assert app.state.store.entries()['entries']==[]
                assert app.state.store.status(device['device_id'])['reserved_bytes']==0
    finally:
        daemon.terminate();daemon.wait(timeout=5)
    return 0

if __name__=='__main__':
    if '--isolated' not in sys.argv:
        raise SystemExit(subprocess.run(['dbus-run-session','--',sys.executable,__file__,'--isolated']).returncode)
    with tempfile.TemporaryDirectory(prefix='gvfs-cloud-write-check-') as directory:
        raise SystemExit(main(Path(directory)))
