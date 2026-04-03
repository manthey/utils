#!/usr/bin/env python3
# /// script
# dependencies = [
#   "requests>=2.31.0",
#   "PyGithub>=2.1.1",
# ]
# ///

import argparse
from pathlib import Path

from github import Auth, Github
from github.GithubException import GithubException

ShortTypes = {
    'oi': 'open-issues',
    'ci': 'closed-issues',
    'op': 'open-prs',
    'mp': 'merged-prs',
    'ap': 'abandoned-prs',
}


def get_state_label(item_type):
    state_labels = {
        'open-issues': 'Open',
        'closed-issues': 'Closed',
        'open-prs': 'Open',
        'merged-prs': 'Merged',
        'abandoned-prs': 'Abandoned',
    }
    return state_labels[item_type]


def format_issue_content(issue, state_label, repository):
    content_lines = [
        f'# [{state_label}] {issue.title} - Issue #{issue.number} - {repository}',
        '',
        f'**Repository**: {repository}',
        f'**Number:** {issue.number}',
        f'**Author:** {issue.user.login}',
        f'**Created:** {issue.created_at}',
        f'**Updated:** {issue.updated_at}',
        '',
    ]
    if issue.body:
        content_lines.extend(['## Description', '', issue.body, ''])
    comments = issue.get_comments()
    if comments.totalCount > 0:
        content_lines.extend(['## Comments', ''])
        for comment in comments:
            content_lines.extend([
                f'### {comment.user.login} - {comment.created_at}',
                '',
                comment.body,
                '',
            ])
    return '\n'.join(content_lines)


def format_pull_request_content(pull_request, state_label, include_diff, repository):
    content_lines = [
        (f'# [{state_label}] {pull_request.title} - Pull Request '
         f'#{pull_request.number} - {repository}'),
        '',
        f'**Repository**: {repository}',
        f'**Number:** {pull_request.number}',
        f'**Author:** {pull_request.user.login}',
        f'**Created:** {pull_request.created_at}',
        f'**Updated:** {pull_request.updated_at}',
        f'**Base:** {pull_request.base.ref}',
        f'**Head:** {pull_request.head.ref}',
        '',
    ]
    if pull_request.body:
        content_lines.extend(['## Description', '', pull_request.body, ''])
    comments = pull_request.get_comments()
    issue_comments = pull_request.get_issue_comments()
    all_comments = []
    for comment in comments:
        all_comments.append((comment.created_at, 'review', comment))
    for comment in issue_comments:
        all_comments.append((comment.created_at, 'issue', comment))
    all_comments.sort(key=lambda x: x[0])
    if all_comments:
        content_lines.extend(['## Comments', ''])
        for created_at, _comment_type, comment in all_comments:
            content_lines.extend([
                f'### {comment.user.login} - {created_at}',
                '',
                comment.body,
                '',
            ])
    if include_diff:
        try:
            diff_content = pull_request.get_files()
            content_lines.extend(['## Diff', ''])
            for file in diff_content:
                content_lines.extend([
                    f'### {file.filename}',
                    '',
                    f'**Status:** {file.status}',
                    f'**Additions:** {file.additions}, **Deletions:** {file.deletions}',
                    '',
                ])
                if file.patch:
                    content_lines.extend(['```diff', file.patch, '```', ''])
        except GithubException:
            content_lines.extend(['## Diff', '', 'Diff content not available', ''])
    return '\n'.join(content_lines)


def download_items(repository, types, output_directory, include_diff, github_client):
    repository_name = repository.split('https://github.com/')[-1].rstrip('/')
    repo = github_client.get_repo(repository_name)
    output_path = Path(output_directory)
    output_path.mkdir(parents=True, exist_ok=True)
    for type_val in types.split(','):
        item_type = ShortTypes.get(type_val, type_val)
        state_label = get_state_label(item_type)
        if 'issues' in item_type:
            state = 'open' if item_type == 'open-issues' else 'closed'
            issues = repo.get_issues(state=state)
            for issue in issues:
                if issue.pull_request:
                    continue
                content = format_issue_content(issue, state_label, repository)
                filename = output_path / f'issue_{issue.number}.md'
                with open(filename, 'w', encoding='utf-8') as file:
                    file.write(content)
                print(f'Downloaded issue #{issue.number}')
        elif 'prs' in item_type:
            if item_type == 'open-prs':
                pulls = repo.get_pulls(state='open')
            elif item_type == 'merged-prs':
                pulls = [pr for pr in repo.get_pulls(state='closed') if pr.merged]
            elif item_type == 'abandoned-prs':
                pulls = [pr for pr in repo.get_pulls(state='closed') if not pr.merged]
            for pull_request in pulls:
                content = format_pull_request_content(
                    pull_request, state_label, include_diff, repository)
                filename = output_path / f'pr_{pull_request.number}.md'
                with open(filename, 'w', encoding='utf-8') as file:
                    file.write(content)
                print(f'Downloaded PR #{pull_request.number}')


def main():
    parser = argparse.ArgumentParser(
        description='Download GitHub issues and pull requests as markdown files',
    )
    parser.add_argument(
        'repository', help='GitHub repository in format owner/repo or full URL',
    )
    parser.add_argument(
        '--type', '-t',
        default='open-issues',
        help='Types of items to download.  A comma separated list of '
        '"open-issues,closed-issues,open-prs,merged-prs,abandoned-prs" or '
        '"oi,ci,op,mp,ap"',
    )
    parser.add_argument(
        '--token',
        help='GitHub personal access token for authentication',
    )
    parser.add_argument(
        '--output-dir', '-o', default='output',
        help='Directory to save markdown files (default: output)',
    )
    parser.add_argument(
        '--include-diff', '-d', action='store_true',
        help='Include diff content for pull requests',
    )
    arguments = parser.parse_args()
    github_client = Github(auth=Auth.Token(arguments.token)) if arguments.token else Github()
    download_items(
        arguments.repository,
        arguments.type,
        arguments.output_dir,
        arguments.include_diff,
        github_client,
    )
    print(f'Download complete. Files saved to {arguments.output_dir}')


if __name__ == '__main__':
    main()
