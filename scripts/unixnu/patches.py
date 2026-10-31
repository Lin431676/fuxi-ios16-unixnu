"""Ordered XNU patch series and target-local source preparation."""
import hashlib
import json
import os
import shutil

from .profiles import resolve_path
from .util import Error, git, log, run


def plan(ctx, modules):
    result = []
    for reference in ctx.board.get('kernel', {}).get('patch_series', []):
        series = resolve_path(ctx, reference, modules)
        if not series.is_file():
            raise Error(f'patch series missing: {series}')
        for number, line in enumerate(series.read_text().splitlines(), 1):
            entry = line.strip()
            if not entry or entry.startswith('#'):
                continue
            if entry.startswith('component:') or entry.partition(':')[0] in ctx.profile_roots:
                path = resolve_path(ctx, entry, modules)
            else:
                path = series.parent / entry
            path = path.resolve()
            if not path.is_file():
                raise Error(f'{series}:{number}: patch missing: {path}')
            if path in result:
                raise Error(f'patch appears twice in the selected series: {path}')
            result.append(path)
    return result


def prepare(ctx, modules):
    selected = plan(ctx, modules)
    module = modules.get(('refer', 'xnu'))
    if not module or module.path is None:
        raise Error('XNU source missing; run unixnu fetch')
    revision = git(module.path, 'rev-parse', 'HEAD').strip()
    # A series may also name shell scripts: a device stages the upstream sources a later patch edits
    # (libpthread, BearSSL, HFS) by running them in the work tree before the patches are applied.
    # The variables they need are the device's to declare, in [kernel] patch_env.
    stage_env = {str(name): str(value) for name, value in ctx.board.get('kernel', {}).get('patch_env', {}).items()}
    signature = {'source': str(module.path), 'commit': revision, 'stage_env': stage_env,
                 'patches': [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                             for path in selected]}
    work = ctx.tree / '.cache/xnu-work'
    stamp = ctx.work / 'xnu-inputs.json'
    try:
        previous = json.loads(stamp.read_text())
    except (FileNotFoundError, ValueError):
        previous = None
    if previous != signature or not (work / '.git').is_dir():
        log(f'kernel: prepare XNU {revision[:12]}')
        stamp.unlink(missing_ok=True)
        if work.exists():
            shutil.rmtree(work)
        work.parent.mkdir(parents=True, exist_ok=True)
        run(['git', 'clone', '--quiet', '--shared', '--no-checkout', module.path, work])
        git(work, 'checkout', '--quiet', '--detach', revision)
        for path in selected:
            if path.suffix == '.sh':
                log(f'kernel: stage {path.name}')
                try:
                    run(['bash', str(path)], cwd=work, env={**os.environ, **stage_env})
                except Error as error:
                    shutil.rmtree(work)
                    raise Error(f'XNU stage {path}: {error}') from error
                continue
            log(f'kernel: patch {path.name}')
            try:
                git(work, 'apply', str(path))
            except Error as error:
                shutil.rmtree(work)
                raise Error(f'XNU patch {path}: {error}') from error
        # A fresh clone lacks the public headers newer xnu omits (TrustCache among them); archives compile against it before the kernel build.
        run(['python3', ctx.tree / 'scripts/prepare-xnu-source.py'], cwd=ctx.tree)
        stamp.write_text(json.dumps(signature, indent=2) + '\n')
    patch_list = ctx.work / 'xnu-patches.list'
    patch_list.write_text(''.join(str(path) + '\n' for path in selected if path.suffix == '.patch'))
    return patch_list
