#!/usr/bin/env python3
"""Move the test runtime and data together, select deriw-dev, and apply v10."""
from copy import deepcopy
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy_testnet_v10 as T

FORMAT = 'deriw-test-v10-relocated-v2'
LABEL = 'org.deriw.test-runtime-layout'
INIT = {'db-init', 'stats-db-init'}


def git(repo, *args):
    return T.P.run(['git', '-C', str(repo), *args]).strip()


def inside(path, parent):
    return Path(path).is_absolute() and Path(path).is_relative_to(parent)


def remap(path, old, new):
    return str(new / Path(path).relative_to(old)) if inside(path, old) else path


def identity(path):
    value = Path(path).stat()
    return [value.st_dev, value.st_ino]


def mounted_children(old, raw=None):
    if raw is None:
        if not sys.platform.startswith('linux'):
            return []
        raw = Path('/proc/self/mountinfo').read_text()
    result = []
    for line in raw.splitlines():
        encoded = line.split()[4]
        mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), encoded)
        if inside(mount, old):
            result.append(mount)
    return result


def outside_users(old, own_ids):
    ids = T.P.run(['docker', 'ps', '-aq']).split()
    items = json.loads(T.P.run(['docker', 'inspect', *ids])) if ids else []
    return [{'id': c['Id'], 'name': c['Name'], 'sources': [m['Source'] for m in c['Mounts']
             if m['Type'] == 'bind' and inside(m['Source'], old)]}
            for c in items if c['Id'] not in own_ids and any(
                m['Type'] == 'bind' and inside(m['Source'], old) for m in c['Mounts'])]


def check_git(repo, target):
    branch = git(repo, 'branch', '--show-current')
    T.require(branch in ('deriw-pre', 'deriw-dev'), 'Select the existing test checkout on deriw-pre or deriw-dev.')
    T.require(not git(repo, 'diff', '--name-only', 'HEAD', '--', '.', ':(exclude)docker-compose'),
              'Preserve tracked edits outside docker-compose before changing the test branch.')
    changed = git(repo, 'diff', '--name-only', T.SOURCE, target, '--', 'apps', 'config', 'docker', 'rel', 'mix.exs', 'mix.lock')
    T.require(not changed, 'The selected deriw-dev application differs from the qualified v10 source.')
    worktrees = git(repo, 'worktree', 'list', '--porcelain').splitlines()
    T.require(branch == 'deriw-dev' or 'branch refs/heads/deriw-dev' not in worktrees,
              'deriw-dev is already checked out in another worktree.')
    local = git(repo, 'for-each-ref', '--format=%(objectname)', 'refs/heads/deriw-dev')
    if local:
        result = subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', local, target], capture_output=True)
        T.require(result.returncode == 0, 'The local deriw-dev branch cannot fast-forward; no branch will be reset.')
    return {'original_branch': branch, 'original_head': git(repo, 'rev-parse', 'HEAD'),
            'local_dev_head': local, 'target_head': target}


def plan(repo, model, rows, project):
    old, new = repo / 'docker-compose', repo / 'docker-compose.test'
    T.require(old.is_dir() and not old.is_symlink() and not os.path.lexists(new),
              'The initial relocation requires the original docker-compose tree and an absent docker-compose.test.')
    T.require(old.stat().st_dev == repo.stat().st_dev, 'Runtime relocation requires a same-filesystem directory rename.')
    T.require(old.stat().st_uid == os.geteuid(), 'The original runtime directory must belong to the deployment operator.')
    T.require(not mounted_children(old), 'Host filesystem mounts inside the runtime tree need their mount definitions reviewed before rename.')
    T.require(set(model['services']) == set(rows), 'Every captured Compose service must be accounted for before relocation.')
    source = check_git(repo, git(repo, 'rev-parse', 'refs/remotes/origin/deriw-dev'))
    mounts = {}
    for name, row in rows.items():
        T.validate_topology(model, row, name, rows=rows)
        labels = row['Config'].get('Labels') or {}
        for filename in labels.get('com.docker.compose.project.config_files', '').split(','):
            T.require(inside(filename, old), 'A test service uses Compose files outside the runtime directory.')
        for mount in row['Mounts']:
            if mount['Type'] == 'bind' and inside(mount['Source'], old):
                path = Path(mount['Source'])
                T.require(path.resolve(strict=True) == path, 'Resolve symlinked runtime bind sources before relocation.')
                mounts[str(path)] = identity(path)
        if name not in INIT:
            T.require(row['State']['Running'] or row['State'].get('Restarting'), 'An expected test service is stopped before relocation.')
    for name in ('db', 'stats-db', 'redis-db'):
        T.require(name in rows and any(m['Type'] == 'bind' and inside(m['Source'], old) for m in rows[name]['Mounts']),
                  'Expected the test database/Redis data within the original runtime directory.')
        for mount in rows[name]['Mounts']:
            if mount['Type'] == 'bind' and mount['RW']:
                T.require(inside(mount['Source'], old), 'A database/Redis writable bind lies outside the runtime tree.')
                T.require(not git(repo, 'ls-files', '--', str(Path(mount['Source']).relative_to(repo))),
                          'A database/Redis data bind contains tracked files; review before restoring public templates.')
    for volume in model.get('volumes', {}).values():
        T.require(not any(inside(str(v), old) for v in (volume.get('driver_opts') or {}).values()),
                  'A named volume embeds the old host path and needs separate review.')
    foreign = outside_users(old, {row['Id'] for row in rows.values()})
    T.require(not foreign, 'Another container binds the runtime directory; resolve that attachment before relocation.')
    return {**source, 'old': str(old), 'new': str(new), 'root_identity': identity(old),
            'original_main_sha256': T.R.digest((old / 'docker-compose.yml').read_bytes()),
            'bind_identities': mounts, 'project': project, 'stopped_init_services': sorted(INIT & set(rows)),
            'running_services': sorted(set(rows) - INIT), 'directory_moved': False, 'preparation_complete': False}


def relocated_model(original, rows, path, layout):
    model = deepcopy(original)
    old, new = Path(layout['old']), Path(layout['new'])
    for name, service in model['services'].items():
        row = rows[name]
        if name != 'backend':
            service['image'] = row['Image']
            service['environment'] = T.D.env_map(row)
            service['command'] = row['Config'].get('Cmd') or []
            service['entrypoint'] = row['Config'].get('Entrypoint') or []
            if row['Config'].get('WorkingDir'):
                service['working_dir'] = row['Config']['WorkingDir']
            if row['Config'].get('User'):
                service['user'] = row['Config']['User']
        service.pop('build', None)
        # Values are frozen from the actual container; the preserved env files
        # move with the directory but need not be re-read before that rename.
        service.pop('env_file', None)
        service['pull_policy'] = 'never'
        service.setdefault('labels', {})[LABEL] = str(path)
        for mount in service.get('volumes') or []:
            if mount['type'] == 'bind':
                mount['source'] = remap(mount['source'], old, new)
                mount.setdefault('bind', {})['create_host_path'] = False
        # Preserve even dynamically assigned addresses. Otherwise an early
        # dynamic service can occupy the old address of a static DB service.
        for key, options in service.get('networks', {}).items():
            name_on_host = model['networks'][key]['name']
            live = row['NetworkSettings']['Networks'][name_on_host]
            options = dict(options or {})
            if live.get('IPAddress'):
                options['ipv4_address'] = live['IPAddress']
            if live.get('GlobalIPv6Address'):
                options['ipv6_address'] = live['GlobalIPv6Address']
            service['networks'][key] = options
    for category in ('configs', 'secrets'):
        for item in model.get(category, {}).values():
            if item.get('file'):
                item['file'] = remap(item['file'], old, new)
    return model


def prepare(repo, preflight, backup, journal):
    project, rows = T.P.select_project('backend')
    captured = (preflight / 'compose.private.json').read_text()
    if T.D.compose_json_escapes_dollars():
        captured = captured.replace('$$', '$')
    layout = plan(repo, json.loads(captured), rows, project)
    path, state = T.prepare(repo, preflight, backup, journal)
    state.update(format=FORMAT, layout=layout, tool_script=str(Path(__file__).resolve()))
    T.save(path, state, 'preparing_runtime_relocation')
    T.require(inside(state['data_host_path'], Path(layout['old'])), 'The inspected main database data is outside the runtime tree.')
    original = json.loads((path.parent / 'candidate.compose.json').read_text().replace('$$', '$'))
    model = relocated_model(original, rows, path, layout)
    selected = path.parent / 'relocated.compose.json'
    T.R.write_new(selected, T.R.snapshot_bytes(model))
    resolved = T.D.compose_model(Path(layout['new']), {'compose_project': project}, {}, [selected], True)
    T.require(not T.D.model_differences(model, resolved), 'Compose changed the prepared relocated configuration.')
    state['relocated_sha256'] = T.R.digest(selected.read_bytes())
    layout['preparation_complete'] = True
    T.save(path, state, 'prepared')
    T.emit('test_relocation_prepared', state=str(path), source_branch='deriw-dev',
           runtime_directory=layout['new'], data_copy_required=False, services_restarted=False)
    return path, state


def original_containers(state, rows):
    layout = state['layout']
    T.require(set(rows) == set(state['before']), 'The original test service inventory changed before relocation.')
    for name, before in state['before'].items():
        row = rows[name]
        expected = deepcopy(before['HostConfig'])
        if name == 'backend' or layout.get('stop_requested'):
            expected['RestartPolicy'] = row['HostConfig']['RestartPolicy']
            T.require(row['HostConfig']['RestartPolicy'] in (before['HostConfig']['RestartPolicy'], {'Name':'no','MaximumRetryCount':0}),
                      'A test restart policy changed unexpectedly.')
        T.require(row['Id'] == before['Id'] and row['Image'] == before['Image']
                  and row['Config'] == before['Config'] and row['HostConfig'] == expected
                  and T.D.inspect_mounts(row['Mounts']) == T.D.inspect_mounts(before['Mounts']),
                  'An original service changed outside the recorded deployment.')


def schema(path, state, rows):
    if state['schema_complete']:
        return
    if not state.get('handoff'):
        T.require(rows['backend']['State']['Running'], 'The v9 backend must run until both bridge cursors are saved.')
        state['handoff'] = T.rpc(rows['backend'], T.cursor_expression(),
                                 path.parent / ('bridge-handoff-' + uuid.uuid4().hex[:8] + '.private.log'))
        T.require({r['tracker'] for r in state['handoff']['cursors']} == {'deposits', 'withdrawals'}, 'Both bridge cursors are required.')
        T.save(path, state, 'cursors_saved')
        T.emit('test_bridge_cursors_saved', **state['handoff'])
    T.save(path, state, 'stopping_backend')
    T.change(['docker', 'update', '--restart=no', rows['backend']['Id']], path, 'hold-old-backend-restart')
    T.change(['docker', 'stop', '-t', '300', rows['backend']['Id']], path, 'stop-old-backend', timeout=330)
    T.require(not T.current(state['project'])['backend']['State']['Running'], 'The old backend did not stop.')
    T.client(state, path, 'inspect', state['schema_before'])
    info = os.statvfs(state['data_host_path'])
    state['schema_start_free_bytes'] = info.f_bavail * info.f_frsize
    T.require(state['schema_start_free_bytes'] >= 120 * 1024**3, 'Schema working-space reserve is unavailable.')
    T.save(path, state, 'schema_started')
    T.emit('test_schema_migrating', background_migrators_autostart=False)
    result = T.client(state, path, 'migrate', state['schema_before'])
    T.require(result['heap_identity'] == state['schema_before']['heap_identity']
              and result['pending'] == {'repo': sorted(T.R.VERSIONS), 'account': [], 'arbitrum': []},
              'The schema result differs from the reviewed initial migration path.')
    state['schema_complete'] = True
    T.save(path, state, 'schema_complete')
    T.emit('test_schema_complete', initial_schema_versions=29, historical_heap_rewritten=False)


def move_directory(path, state):
    layout = state['layout']
    old, new = Path(layout['old']), Path(layout['new'])
    if new.exists() and identity(new) == layout['root_identity']:
        layout['directory_moved'] = True
    if not layout['directory_moved']:
        rows = T.current(state['project'])
        original_containers(state, rows)
        layout['stop_requested'] = True
        T.save(path, state, 'stopping_test_runtime')
        ids = [c['Id'] for c in rows.values()]
        T.emit('test_runtime_stopping', services=layout['running_services'])
        T.change(['docker', 'update', '--restart=no', *ids], path, 'hold-runtime-restarts')
        T.change(['docker', 'stop', '-t', '300', *ids], path, 'stop-test-runtime', timeout=360)
        stopped = T.current(state['project'])
        T.require(not any(c['State']['Running'] or c['State'].get('Restarting') for c in stopped.values()),
                  'All project containers must stop before moving their bind sources.')
        for name in ('db', 'stats-db'):
            T.require(stopped[name]['State']['ExitCode'] == 0 and not stopped[name]['State'].get('OOMKilled'),
                      'PostgreSQL did not stop cleanly; keep the runtime tree in place for recovery.')
        T.require(not outside_users(old, set(ids)), 'Another container acquired a runtime bind during deployment.')
        T.require(identity(old) == layout['root_identity'] and not os.path.lexists(new), 'The runtime directory changed before rename.')
        for source, original in layout['bind_identities'].items():
            T.require(identity(source) == original, 'An original bind source changed before rename.')
        T.save(path, state, 'directory_rename_requested')
        os.rename(old, new)
        descriptor = os.open(new.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        layout['directory_moved'] = True
    new.chmod(0o700)
    for source, original in layout['bind_identities'].items():
        T.require(identity(remap(source, old, new)) == original, 'A relocated bind source no longer has its original inode/device.')
    state['data_host_path'] = remap(state['data_host_path'], old, new)
    T.save(path, state, 'runtime_directory_moved')
    T.emit('test_runtime_directory_moved', directory=str(new), bind_identities_preserved=True, data_copied=False)


def switch_branch(path, state):
    layout, repo = state['layout'], Path(state['repo'])
    target = layout['target_head']
    branch, head = git(repo, 'branch', '--show-current'), git(repo, 'rev-parse', 'HEAD')
    if branch != 'deriw-dev' or head != target:
        T.require(head in (layout['original_head'], layout['local_dev_head'], target), 'The source checkout changed during deployment.')
        T.require(not git(repo, 'diff', '--name-only', 'HEAD', '--', '.', ':(exclude)docker-compose'),
                  'Tracked source changed outside the moved runtime tree.')
        T.save(path, state, 'source_switch_requested')
        # All server-owned files, including tracked local configuration edits,
        # are now in the ignored .test tree. Restore only tracked templates in
        # the vacated original path, then perform a non-destructive Git switch.
        T.change(['git', '-C', str(repo), 'restore', '--source=HEAD',
                  '--staged', '--worktree', '--', 'docker-compose'], path, 'restore-public-compose')
        local = git(repo, 'for-each-ref', '--format=%(objectname)', 'refs/heads/deriw-dev')
        if not local:
            T.change(['git', '-C', str(repo), 'switch', '-c', 'deriw-dev', target], path, 'select-dev-branch')
        else:
            T.change(['git', '-C', str(repo), 'switch', 'deriw-dev'], path, 'select-dev-branch')
            T.change(['git', '-C', str(repo), 'merge', '--ff-only', target], path, 'advance-dev-branch')
        T.change(['git', '-C', str(repo), 'branch', '--set-upstream-to=origin/deriw-dev', 'deriw-dev'], path, 'track-dev-branch')
    T.require(git(repo, 'branch', '--show-current') == 'deriw-dev' and git(repo, 'rev-parse', 'HEAD') == target,
              'The requested deriw-dev checkout was not selected.')
    T.ensure_private_config(repo, Path(layout['new']))
    T.save(path, state, 'source_selected')
    T.emit('test_source_selected', branch='deriw-dev', checkout_revision=target, application_revision=T.SOURCE)


def write_config(path, state):
    selected = path.parent / 'relocated.compose.json'
    T.require(T.R.digest(selected.read_bytes()) == state['relocated_sha256'], 'The prepared relocated configuration changed.')
    target = Path(state['layout']['new']) / 'docker-compose.yml'
    if target.read_bytes() != selected.read_bytes():
        T.require(T.R.digest(target.read_bytes()) == state['layout']['original_main_sha256'],
                  'The runtime Compose file was edited after preparation; preserve that edit before resuming.')
        backup = path.parent / 'moved-original-compose.private.yml'
        if not backup.exists():
            T.R.write_new(backup, target.read_bytes())
        temporary = target.parent / ('.selected-' + uuid.uuid4().hex + '.tmp')
        T.R.write_new(temporary, selected.read_bytes())
        os.replace(temporary, target)
        descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    T.save(path, state, 'relocated_configuration_selected')
    return target


def up(path, state, target, names):
    if names:
        T.emit('test_services_recreating', services=sorted(names))
        T.change(['docker', 'compose', '-p', state['project'], '--project-directory', str(target.parent),
                  '--env-file', os.devnull, '-f', str(target), 'up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                  *sorted(names)], path, 'recreate-services', timeout=360)


def wait_databases(state):
    deadline = time.monotonic() + 300
    while True:
        rows = T.current(state['project'])
        if all(rows[n]['State']['Running'] and rows[n]['State'].get('Health', {}).get('Status') == 'healthy'
               for n in ('db', 'stats-db')):
            return
        T.require(time.monotonic() < deadline, 'The relocated databases did not become healthy.')
        T.emit('waiting_for_relocated_databases')
        time.sleep(5)


def verify_layout(state, rows):
    layout = state['layout']
    expected = set(layout['running_services'])
    T.require(set(rows) == expected, 'The relocated runtime service inventory differs.')
    model = json.loads((Path(state['layout']['new']) / 'docker-compose.yml').read_text().replace('$$', '$'))
    for name in expected:
        row = rows[name]
        service = model['services'][name]
        image = state['image_id'] if name == 'backend' else state['before'][name]['Image']
        T.require(row['Image'] == image and row['Config']['Labels'].get(LABEL) == str(state['state_path']),
                  'A service differs from its selected image/layout.')
        T.validate_topology(model, row, name, rows=rows)
        env = T.D.env_map(row)
        T.require(all(env.get(k) == v for k, v in service.get('environment', {}).items() if v is not None),
                  'A relocated service environment differs from its saved selection.')
        # user-ops was already restarting in the supplied test inventory.
        if not state['before'][name]['State'].get('Restarting'):
            T.require(row['State']['Running'] and not row['State'].get('Restarting'), 'A relocated test service is not running.')
        else:
            T.require(row['State']['Running'] or row['State'].get('Restarting'), 'The existing restarting service was not started.')
    for source, original in layout['bind_identities'].items():
        T.require(identity(remap(source, Path(layout['old']), Path(layout['new']))) == original,
                  'A relocated data/configuration bind changed identity.')


def apply(path, state):
    layout = state['layout']
    T.require(state.get('format') == FORMAT and layout.get('preparation_complete') and state.get('preparation_complete'),
              'Runtime relocation preparation did not complete.')
    state['state_path'] = str(path)
    image = json.loads(T.P.run(['docker', 'image', 'inspect', state['image_tag']]))[0]
    T.require(image['Id'] == state['image_id'] and image['Config']['Labels']['org.opencontainers.image.revision'] == T.SOURCE,
              'The prepared backend image tag changed.')
    # Recover the atomic rename even if the process stopped before its journal update.
    new = Path(layout['new'])
    moved = new.exists() and identity(new) == layout['root_identity']
    T.require(not os.path.lexists(new) or moved, 'Another directory occupies docker-compose.test; no schema writes or rename will be attempted.')
    check_git(Path(state['repo']), layout['target_head'])
    if not moved:
        T.require(T.R.digest((Path(layout['old']) / 'docker-compose.yml').read_bytes()) == layout['original_main_sha256'],
                  'The original Compose file changed after preparation; prepare a new plan before any schema writes.')
        rows = T.current(state['project'])
        original_containers(state, rows)
        schema(path, state, rows)
    else:
        T.require(state['schema_complete'], 'The runtime directory moved before the recorded schema stage completed.')
    move_directory(path, state)
    switch_branch(path, state)
    target = write_config(path, state)
    rows = T.current(state['project'])
    for name in layout['stopped_init_services']:
        if name in rows:
            row = rows[name]
            T.require(row['Id'] == state['before'][name]['Id'] and not row['State']['Running'], 'A stopped initializer changed.')
            T.change(['docker', 'rm', row['Id']], path, 'remove-stopped-initializer')
    up(path, state, target, ['db', 'stats-db', 'redis-db'])
    wait_databases(state)
    T.client(state, path, 'inspect', state['schema_before'])
    remaining = set(layout['running_services']) - {'db', 'stats-db', 'redis-db', 'backend', 'proxy'}
    up(path, state, target, remaining)
    up(path, state, target, ['backend'])
    deadline = time.monotonic() + 180
    while True:
        backend = T.current(state['project'])['backend']
        try:
            T.require(T.P.run(['docker', 'exec', backend['Id'], 'curl', '-fsS', '--max-time', '3',
                              'http://127.0.0.1:4000/api/health/readiness'], timeout=6).strip() == 'OK', 'Backend is starting.')
            break
        except T.P.PreflightError:
            T.require(time.monotonic() < deadline, 'The relocated backend did not become ready.')
            T.emit('waiting_for_test_backend')
            time.sleep(5)
    up(path, state, target, ['proxy'] if 'proxy' in layout['running_services'] else [])
    runtime = T.verify_backend(path, state, verify_layout)
    T.save(path, state, 'active')
    T.emit('test_v10_deployed', source_revision=T.SOURCE, image=state['image_tag'], state=str(path),
           source_branch='deriw-dev', checkout_revision=layout['target_head'], private_compose=str(target),
           runtime_and_data_directory=str(new), data_copied=False, background_migrators_autostart=False, **runtime)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path('/data/blockscout'))
    parser.add_argument('--preflight', type=Path)
    parser.add_argument('--existing-backup', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--resume', type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    path, state, journal = None, None, {}
    try:
        T.check_host()
        with (T.BASE / 'upgrade.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise T.P.PreflightError('Another test upgrade holds the deployment lock.') from None
            if args.resume:
                path = args.resume.resolve(strict=True)
                T.require(path.name == 'state.json' and path.parent.parent == T.BASE.resolve()
                          and path.stat().st_mode & 0o077 == 0 and not args.resume.is_symlink(), 'Select this tool\'s private journal.')
                state = json.loads(path.read_text())
                T.require(state.get('format') == FORMAT and state.get('source') == T.SOURCE
                          and state.get('existing_backup_recorded') and state.get('layout', {}).get('preparation_complete'),
                          'Select a completed v2 relocation preparation; earlier backend-only journals cannot move data.')
            else:
                T.require(args.preflight is not None, 'Select the successful test preflight with --preflight.')
                path, state = prepare(args.repo.resolve(), args.preflight.resolve(), args.existing_backup, journal)
            if not args.prepare_only:
                apply(path, state)
        return 0
    except Exception as error:
        path, state = journal.get('path', path), journal.get('state', state)
        if path is not None and state is not None:
            T.P.private_write(path.parent / ('failure-' + uuid.uuid4().hex[:8] + '.private.log'), traceback.format_exc())
            state['failed_phase'] = state.get('phase')
            T.save(path, state, 'needs_attention')
            T.emit('test_release_needs_attention', state=str(path), failed_phase=state['failed_phase'])
        print('STOP:', str(error) if isinstance(error, T.P.PreflightError) else 'Release failed; raw diagnostics remain private.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
