'''
WebUI v2 Updater -- opt-in installer/updater for the nodel-webui-v2 overlay.

Manages ONLY the v2 file set (entry pages + the v2/ support tree) inside
<hostroot>/custom/content, leaving any unrelated custom content and all
embedded v1 content untouched. index.htm is withheld by default per the
upstream preserve-v1 collision policy.

Every install is verified twice before going live: the release ZIP against
its published .sha256 asset, then every extracted file against the per-file
SHA-256 inventory inside the release's own release.json. Installs use a
staged swap (directory renames on the same filesystem); the previous
version is kept for Rollback, which is its own inverse.

The check timer only notifies (UpdateAvailable event) -- it never installs.
Interim tooling for the alpha/beta period until webui-v2 ships inside the
Nodel jar; once that happens, retire this node.
'''

import os
import jarray
from java.io import File, FileInputStream, FileOutputStream, BufferedInputStream, BufferedOutputStream
from java.net import URL
from java.security import MessageDigest
from java.util.zip import ZipFile
from org.nodel.io import Stream

DEFAULT_REPO = 'mcartmel/nodel-webui-v2'
UA = 'nodel-webui-v2-updater'
ENTRY_PAGES = ['nodes.html', 'nodel.html', 'toolkit.html', 'components.html']
INDEX_PAGE = 'index.htm'
STATE_DIR = '.webui-v2-updater'

param_repo = Parameter({
    'title': 'GitHub repository (owner/name)',
    'schema': {'type': 'string', 'hint': DEFAULT_REPO},
    'order': 1})

param_pinnedVersion = Parameter({
    'title': 'Pinned version tag (blank = latest stable release)',
    'schema': {'type': 'string', 'hint': 'e.g. v0.1.2'},
    'order': 2})

param_checkIntervalHours = Parameter({
    'title': 'Update check interval (hours, 0 = disabled)',
    'schema': {'type': 'integer'},
    'default': 24,
    'order': 3})

param_installIndexHtm = Parameter({
    'title': 'Also install index.htm (replaces the v1 landing page!)',
    'schema': {'type': 'boolean'},
    'default': False,
    'order': 4})

param_hostRoot = Parameter({
    'title': 'Host root override (blank = auto-detect)',
    'schema': {'type': 'string'},
    'order': 5})

local_event_Status = LocalEvent({
    'group': 'Status', 'order': 9990,
    'schema': {'type': 'object', 'properties': {
        'level': {'type': 'integer'},
        'message': {'type': 'string'}}}})

local_event_LastChecked = LocalEvent({
    'group': 'Status', 'schema': {'type': 'string'}})

local_event_InstalledVersion = LocalEvent({
    'group': 'Versions', 'order': 1, 'schema': {'type': 'string'}})

local_event_LatestVersion = LocalEvent({
    'group': 'Versions', 'order': 2, 'schema': {'type': 'string'}})

local_event_UpdateAvailable = LocalEvent({
    'group': 'Versions', 'order': 3, 'schema': {'type': 'boolean'}})

_busy = False


# --- filesystem helpers ---

def _host_root():
    if not is_blank(param_hostRoot):
        root = File(param_hostRoot)
    else:
        # node lives at <hostroot>/nodes/<name>
        root = _node.getRoot().getParentFile().getParentFile()

    if not File(root, 'custom/content').isDirectory():
        raise Exception('could not locate host root (no custom/content under "%s"); '
                        'set the Host Root parameter' % root)
    return root

def _paths():
    root = _host_root()
    state = File(root, 'custom/' + STATE_DIR)
    return {'content': File(root, 'custom/content'),
            'state': state,
            'downloads': File(state, 'downloads'),
            'staging': File(state, 'staging'),
            'backup': File(state, 'backup'),
            'marker': File(state, 'installed.json')}

def _page_set():
    pages = list(ENTRY_PAGES)
    if param_installIndexHtm == True:
        pages.append(INDEX_PAGE)
    return pages

def _rm_tree(f):
    if not f.exists():
        return
    if f.isDirectory():
        kids = f.listFiles()
        if kids is not None:
            for k in kids:
                _rm_tree(k)
    if not f.delete():
        raise Exception('could not delete "%s"' % f)

def _move(src, dst):
    dst.getParentFile().mkdirs()
    if not src.renameTo(dst):
        raise Exception('could not move "%s" to "%s"' % (src, dst))

def _copy_stream(ins, outs):
    buf = jarray.zeros(16384, 'b')
    while True:
        n = ins.read(buf)
        if n < 0:
            break
        outs.write(buf, 0, n)

def _write_text(f, text):
    f.getParentFile().mkdirs()
    fh = open(f.getAbsolutePath(), 'wb')
    try:
        fh.write(text)
    finally:
        fh.close()

def _sha256_file(f):
    md = MessageDigest.getInstance('SHA-256')
    ins = BufferedInputStream(FileInputStream(f))
    try:
        buf = jarray.zeros(16384, 'b')
        while True:
            n = ins.read(buf)
            if n < 0:
                break
            md.update(buf, 0, n)
    finally:
        ins.close()
    return ''.join(['%02x' % (b & 0xff) for b in md.digest()])


# --- GitHub access ---

def _repo():
    return param_repo if not is_blank(param_repo) else DEFAULT_REPO

def _api_get(path):
    raw = get_url('https://api.github.com/%s' % path,
                  headers={'User-Agent': UA, 'Accept': 'application/vnd.github+json'},
                  connectTimeout=10, readTimeout=30)
    return json_decode(raw)

def _resolve_release(tag):
    if is_blank(tag):
        return _api_get('repos/%s/releases/latest' % _repo())
    return _api_get('repos/%s/releases/tags/%s' % (_repo(), tag))

def _download(url, destFile):
    conn = URL(url).openConnection()
    conn.setConnectTimeout(15000)
    conn.setReadTimeout(120000)
    conn.setRequestProperty('User-Agent', UA)
    ins = BufferedInputStream(conn.getInputStream())
    try:
        destFile.getParentFile().mkdirs()
        outs = BufferedOutputStream(FileOutputStream(destFile))
        try:
            _copy_stream(ins, outs)
        finally:
            outs.close()
    finally:
        ins.close()


# --- verification ---

def _extract_zip(zipPath, destDir):
    destDir.mkdirs()
    destBase = destDir.getCanonicalPath()
    zf = ZipFile(zipPath)
    try:
        entries = zf.entries()
        count = 0
        while entries.hasMoreElements():
            entry = entries.nextElement()
            name = entry.getName()
            # strict: reject anything resembling traversal or absolute paths
            if name.startswith('/') or '..' in name or '\\' in name:
                raise Exception('unsafe path in archive: "%s"' % name)
            target = File(destDir, name)
            if not target.getCanonicalPath().startswith(destBase + os.sep):
                raise Exception('archive path escapes staging: "%s"' % name)
            if entry.isDirectory():
                target.mkdirs()
                continue
            target.getParentFile().mkdirs()
            ins = zf.getInputStream(entry)
            try:
                outs = BufferedOutputStream(FileOutputStream(target))
                try:
                    _copy_stream(ins, outs)
                finally:
                    outs.close()
            finally:
                ins.close()
            count += 1
        return count
    finally:
        zf.close()

def _verify_inventory(stagingDir, release):
    if release.get('inventoryAlgorithm') != 'sha256':
        raise Exception('unsupported inventory algorithm "%s"' % release.get('inventoryAlgorithm'))

    expected = {}
    for entry in release.get('files') or []:
        expected[entry['path']] = entry['sha256']
    if len(expected) == 0:
        raise Exception('release.json contains no file inventory')

    excludes = release.get('inventoryExcludes') or []

    for path in expected.keys():
        f = File(stagingDir, path)
        if not f.isFile():
            raise Exception('inventory file missing from archive: "%s"' % path)
        actual = _sha256_file(f)
        if actual != expected[path]:
            raise Exception('hash mismatch for "%s"' % path)

    base = stagingDir.getAbsolutePath()
    unexpected = []
    for dirpath, dirnames, filenames in os.walk(base):
        for name in filenames:
            rel = os.path.join(dirpath, name)[len(base) + 1:].replace('\\', '/')
            if rel not in expected and rel not in excludes:
                unexpected.append(rel)
    if len(unexpected) > 0:
        raise Exception('unexpected files in archive: %s' % ', '.join(unexpected[:5]))

    return len(expected)


# --- install / rollback ---

def _installed_info():
    p = _paths()
    if not p['marker'].isFile():
        return None
    try:
        return json_decode(Stream.readFully(p['marker']))
    except Exception, e:
        console.warn('could not read installed marker: %s' % e)
        return None

def _emit_installed():
    info = _installed_info()
    if info is not None:
        local_event_InstalledVersion.emitIfDifferent(info.get('tag') or 'unknown')
    elif File(_paths()['content'], 'v2').isDirectory():
        local_event_InstalledVersion.emitIfDifferent('unmanaged')
    else:
        local_event_InstalledVersion.emitIfDifferent('not installed')

def _install_staged(stagingDir, release, tag):
    p = _paths()
    pages = _page_set()

    _rm_tree(p['backup'])
    p['backup'].mkdirs()

    # capture current state into backup (marker included, absent if unmanaged)
    currentV2 = File(p['content'], 'v2')
    if currentV2.exists():
        _move(currentV2, File(p['backup'], 'v2'))
    for page in pages:
        current = File(p['content'], page)
        if current.isFile():
            _move(current, File(p['backup'], page))
    if p['marker'].isFile():
        _move(p['marker'], File(p['backup'], 'installed.json'))

    # bring the verified staged tree live
    _move(File(stagingDir, 'v2'), File(p['content'], 'v2'))
    for page in pages:
        staged = File(stagingDir, page)
        if staged.isFile():
            _move(staged, File(p['content'], page))

    _write_text(p['marker'], json_encode({
        'tag': tag,
        'version': release.get('version'),
        'commit': release.get('commit'),
        'repo': _repo(),
        'installedAt': str(date_now())}))


# --- actions ---

@local_action({'group': 'Updater', 'order': 1})
def CheckForUpdates(arg=None):
    _do_check()

def _do_check():
    if _busy:
        console.warn('busy - ignoring check request')
        return
    try:
        release = _resolve_release(param_pinnedVersion)
        latestTag = release.get('tag_name')
        local_event_LatestVersion.emitIfDifferent(latestTag)
        local_event_LastChecked.emit(str(date_now()))
        _emit_installed()

        installed = local_event_InstalledVersion.getArg()
        available = (installed != latestTag)
        local_event_UpdateAvailable.emitIfDifferent(available)

        if available:
            console.info('update available: installed=%s, latest=%s (use the Update action to install)'
                         % (installed, latestTag))
            local_event_Status.emit({'level': 1, 'message': 'Update available: %s (installed: %s)'
                                     % (latestTag, installed)})
        else:
            console.info('up to date (%s)' % latestTag)
            local_event_Status.emit({'level': 0, 'message': 'Up to date (%s)' % latestTag})

    except Exception, e:
        console.error('check failed: %s' % e)
        local_event_Status.emit({'level': 2, 'message': 'Check failed: %s' % e})

@local_action({'group': 'Updater', 'order': 2,
               'schema': {'type': 'string', 'title': 'Version tag (blank = pinned/latest)'}})
def Update(arg=None):
    global _busy
    if _busy:
        console.warn('busy - ignoring update request')
        return
    _busy = True
    try:
        tag = arg if not is_blank(arg) else param_pinnedVersion

        console.info('resolving release (%s)...' % (tag if not is_blank(tag) else 'latest stable'))
        ghRelease = _resolve_release(tag)
        tag = ghRelease.get('tag_name')

        installedInfo = _installed_info()
        if installedInfo is not None and installedInfo.get('tag') == tag:
            console.warn('%s is already installed - reinstalling (verified repair)' % tag)

        zipAsset, shaAsset = None, None
        for asset in ghRelease.get('assets') or []:
            if asset['name'].endswith('.zip.sha256'):
                shaAsset = asset
            elif asset['name'].endswith('.zip'):
                zipAsset = asset
        if zipAsset is None or shaAsset is None:
            raise Exception('release %s is missing the .zip or .zip.sha256 asset' % tag)

        p = _paths()
        _rm_tree(p['downloads'])
        _rm_tree(p['staging'])

        console.info('downloading %s (%s bytes)...' % (zipAsset['name'], zipAsset['size']))
        zipFile = File(p['downloads'], zipAsset['name'])
        _download(zipAsset['browser_download_url'], zipFile)
        shaFile = File(p['downloads'], shaAsset['name'])
        _download(shaAsset['browser_download_url'], shaFile)

        if zipFile.length() != zipAsset['size']:
            raise Exception('download size mismatch (%s != %s)' % (zipFile.length(), zipAsset['size']))
        expectedSha = Stream.readFully(shaFile).split()[0].strip().lower()
        actualSha = _sha256_file(zipFile)
        if actualSha != expectedSha:
            raise Exception('ZIP checksum mismatch (expected %s, got %s)' % (expectedSha, actualSha))
        console.info('ZIP checksum verified (%s)' % actualSha[:16])

        fileCount = _extract_zip(zipFile, p['staging'])
        console.info('extracted %s files to staging' % fileCount)

        releaseJsonFile = File(p['staging'], 'release.json')
        if not releaseJsonFile.isFile():
            raise Exception('archive has no release.json')
        release = json_decode(Stream.readFully(releaseJsonFile))
        sourceTag = (release.get('source') or {}).get('tag')
        if sourceTag != tag:
            raise Exception('release.json tag "%s" does not match release tag "%s"' % (sourceTag, tag))

        verified = _verify_inventory(p['staging'], release)
        console.info('per-file inventory verified (%s files)' % verified)

        api = release.get('nodelApi')
        if api is not None:
            console.info('release declares Nodel API compatibility: min %s, below %s'
                         % (api.get('min'), api.get('maxExclusive')))

        _install_staged(p['staging'], release, tag)
        _rm_tree(p['staging'])
        _rm_tree(p['downloads'])

        _emit_installed()
        latest = local_event_LatestVersion.getArg()
        local_event_UpdateAvailable.emitIfDifferent(latest is not None and latest != tag)
        console.info('installed %s (previous version retained for Rollback)' % tag)
        local_event_Status.emit({'level': 0, 'message': 'Installed %s' % tag})

    except Exception, e:
        console.error('update failed: %s' % e)
        local_event_Status.emit({'level': 2, 'message': 'Update failed: %s' % e})
    finally:
        _busy = False

@local_action({'group': 'Updater', 'order': 3})
def Rollback(arg=None):
    global _busy
    if _busy:
        console.warn('busy - ignoring rollback request')
        return
    _busy = True
    try:
        p = _paths()
        backupKids = p['backup'].listFiles() if p['backup'].isDirectory() else None
        if backupKids is None or len(backupKids) == 0:
            console.warn('no backup available to roll back to')
            local_event_Status.emit({'level': 1, 'message': 'No backup available'})
            return

        # exchange current managed set with the backup set (self-inverse)
        temp = File(p['state'], 'swap-tmp')
        _rm_tree(temp)
        temp.mkdirs()

        managed = ['v2'] + ENTRY_PAGES + [INDEX_PAGE]
        for name in managed:
            current = File(p['content'], name)
            if current.exists():
                _move(current, File(temp, name))
        if p['marker'].isFile():
            _move(p['marker'], File(temp, 'installed.json'))

        for backedUp in p['backup'].listFiles() or []:
            if backedUp.getName() == 'installed.json':
                _move(backedUp, p['marker'])
            else:
                _move(backedUp, File(p['content'], backedUp.getName()))

        _rm_tree(p['backup'])
        _move(temp, p['backup'])

        _emit_installed()
        console.info('rolled back; previous set retained (Rollback again to undo)')
        local_event_Status.emit({'level': 0, 'message': 'Rolled back to %s'
                                 % local_event_InstalledVersion.getArg()})

    except Exception, e:
        console.error('rollback failed: %s' % e)
        local_event_Status.emit({'level': 2, 'message': 'Rollback failed: %s' % e})
    finally:
        _busy = False


# --- lifecycle ---

_checkTimer = Timer(_do_check, 24 * 3600, stopped=True)

def main():
    console.info('WebUI v2 Updater starting')

@after_main
def setup():
    try:
        _emit_installed()
    except Exception, e:
        console.warn('could not determine installed version: %s' % e)

    hours = param_checkIntervalHours if param_checkIntervalHours is not None else 24
    if hours > 0:
        _checkTimer.setDelayAndInterval(60, hours * 3600)
        _checkTimer.start()
        console.info('update checks every %s hour(s) (notify only, never auto-installs)' % hours)
    else:
        console.info('scheduled update checks disabled')
