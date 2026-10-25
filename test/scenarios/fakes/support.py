"""Shared observation only: real git, Claim snapshots and atomic call records."""
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import time


def git(*args):
    return subprocess.run([os.environ['SCENARIO_GIT']] + list(args),
                          capture_output=True, text=True, check=True).stdout.strip()


def branches(state):
    # Use the shipped canonical naming, including configurable patterns.
    sys.path.insert(0, os.environ['SCENARIO_LIB'])
    from ralph_iterate import branch_name
    from ralph_review_round import references_story
    config = json.loads(os.environ['SCENARIO_BRANCHING'])
    names = {}
    for issue in state['issues'].values():
        pattern = config.get('feature_pattern', 'feature/{issue}-{slug}') if 'prd' in issue['labels'] else config.get('branch_pattern', 'ralph/{issue}-{slug}')
        names[branch_name(issue, pattern)] = [issue['number']]
    for pr in state['pulls'].values():
        # Captures can carry historical branch names. Their durable Story reference
        # still identifies the issue whose Claim covers the write.
        referenced = [issue['number'] for issue in state['issues'].values()
                      if references_story(pr, issue['number'])]
        if referenced:
            names[pr['headRefName']] = referenced
    return names


def snapshot():
    """Read the remote at an operation; a later release cannot erase it."""
    remote = os.environ['SCENARIO_REMOTE']
    claims = {}
    for line in git('--git-dir', remote, 'for-each-ref', '--format=%(refname) %(objectname)',
                    'refs/ralph/claims/').splitlines():
        ref, oid = line.split()
        claims[ref.rsplit('/', 1)[1]] = json.loads(git('--git-dir', remote, 'show', '-s', '--format=%B', oid))
    return {'slot': os.environ.get('RALPH_SLOT'),
            'holder': socket.gethostname() + ':' + os.environ.get('RALPH_SLOT', ''),
            'at': time.time(), 'claims': claims}


def log(entry):
    # Agents, git and gh may all finish together. A JSON line is one record.
    with open(os.environ['SCENARIO_LOG'], 'a') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(entry) + '\n')


def github_issues(argv, state):
    """Targets of the fake's supported writes; reads/global labels hold none."""
    if argv[0] == 'issue' and argv[1] in ('edit', 'comment', 'close'):
        return [int(argv[2])]
    if not (argv[0] == 'pr' and argv[1] in ('create', 'edit', 'comment', 'merge')
            or argv[0] == 'api' and any(a == '--method' or a.startswith('--method=') for a in argv)):
        return []
    names = branches(state)
    def pr_issues(ref):
        pr = state['pulls'].get(str(ref)) or next(
            (p for p in state['pulls'].values() if p['headRefName'] == ref), None)
        return names.get(pr['headRefName'], []) if pr else []

    if argv[0] == 'pr':
        if argv[1] == 'create':
            return names.get(argv[argv.index('--head') + 1], [])
        if argv[1] in ('edit', 'comment', 'merge'):
            return pr_issues(argv[2])
    method = next((a.split('=', 1)[1] if '=' in a else argv[i + 1]
                   for i, a in enumerate(argv) if a.split('=')[0] == '--method'), 'GET')
    if argv[0] == 'api' and method.upper() != 'GET':
        route = next((a for a in argv if a.startswith('repos/')), '')
        match = re.search(r'/pulls/(\d+)', route)
        if match:
            return pr_issues(match.group(1))
        match = re.search(r'/issues/comments/(\d+)', route)
        if match:
            ident = int(match.group(1))
            for issue in state['issues'].values():
                if any(c['id'] == ident for c in issue['comments']):
                    return [issue['number']]
            for pr in state['pulls'].values():
                if any(c['id'] == ident for c in pr['comments']):
                    return pr_issues(pr['number'])
        match = re.search(r'/statuses/([0-9a-f]+)', route)
        if match:
            issues = set()
            for pr in state['pulls'].values():
                head = pr.get('mergedHeadOid')
                if not head:
                    proc = subprocess.run(
                        [os.environ['SCENARIO_GIT'], '--git-dir',
                         os.environ['SCENARIO_REMOTE'], 'rev-parse', '--verify', '-q',
                         'refs/heads/' + pr['headRefName']], capture_output=True, text=True)
                    head = proc.stdout.strip() if proc.returncode == 0 else None
                if head == match.group(1):
                    issues.update(pr_issues(pr['number']))
            return sorted(issues)
    return []
