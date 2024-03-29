# Copyright 2023 The Oppia Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS-IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""GitHub related commands and functions."""

from __future__ import annotations

import builtins
import collections
import datetime
import logging

from typing import Any, Callable, DefaultDict, Dict, List, Optional, Tuple, Union
from dateutil import parser
import requests
from src import github_domain



_TOKEN = None
GITHUB_GRAPHQL_URL = 'https://api.github.com/graphql'
PULL_REQUESTS_URL_TEMPLATE = 'https://api.github.com/repos/{0}/{1}/pulls'
ISSUE_TIMELINE_URL_TEMPLATE = (
    'https://api.github.com/repos/{0}/{1}/issues/{2}/timeline')
TIMEOUT_SECS = 15
DELETE_COMMENTS_BEFORE_IN_DAYS = 60


def init_service(token: Optional[str]=None) -> None:
    """Initialize service with the given token.

    Args:
        token: str|None. The GitHub token or None if no token is given.

    Raises:
        Exception. Given GitHub token is not valid.
    """
    if token is None or token == '':
        raise builtins.BaseException(
            'Must provide a valid GitHub Personal Access Token.')

    global _TOKEN # pylint: disable=global-statement
    _TOKEN = token


# Here we use type Any because the decorated function can take any number of arguments
# of any type and return a value of any type.
def check_token(func: Callable[..., Any]) -> Callable[..., Any]:
    """A decorator to check whether the service is initialized with
    the token or not.
    """

    # Here we use type Any because the function can take any number of arguments of any
    # type and return a value of any type.
    def execute_if_token_initialized(*args: Any, **kwargs: Any) -> Any:
        """Executes the given function if the token is initialized."""
        if _TOKEN is None:
            raise builtins.BaseException(
                'Initialize the service with github_services.init_service(TOKEN).')
        return func(*args, **kwargs)

    return execute_if_token_initialized


def _get_request_headers() -> Dict[str, str]:
    """Returns the request headers for github-request."""

    return {
        'Accept': 'application/vnd.github.v3+json',
        'Authorization': f'token {_TOKEN}'
    }


@check_token
def get_prs_assigned_to_reviewers(
    org_name: str,
    repo_name: str,
    max_waiting_time_in_hours: int
) -> DefaultDict[str, List[github_domain.PullRequest]]:
    """Fetches all PRs and returns a list of PRs assigned to reviewers.

    Args:
        org_name: str. GitHub organization name.
        repo_name: str. GitHub repository name.
        max_waiting_time_in_hours: int. The maximum time in hours to wait for a review.
            If the waiting time for a PR review has exceeded this limit, the
            corresponding reviewer should be notified.

    Returns:
        dict. A dictionary that represents the reviewer and the PRs, the reviewer
        is assigned to.
    """

    pr_url = PULL_REQUESTS_URL_TEMPLATE.format(org_name, repo_name)
    reviewer_to_assigned_prs: (
        DefaultDict[str, List[github_domain.PullRequest]]) = (
        collections.defaultdict(list))

    page_number = 1
    while True:
        logging.info('Fetching Pull requests')
        params: Dict[str, Union[str, int]] = {
            'page': page_number, 'per_page': 100, 'status': 'open'
        }
        response = requests.get(
            pr_url,
            params=params,
            headers=_get_request_headers(),
            timeout=TIMEOUT_SECS
        )
        response.raise_for_status()
        pr_subset = response.json()

        if len(pr_subset) == 0:
            break
        page_number += 1

        pull_requests: List[github_domain.PullRequest] = [
            get_pull_request_object_from_dict(org_name, repo_name, pull_request)
            for pull_request in pr_subset
        ]

        for pull_request in pull_requests:
            if not pull_request.is_reviewer_assigned():
                continue
            for reviewer in pull_request.assignees:
                pending_review_time = (
                    datetime.datetime.now(datetime.timezone.utc) -
                    reviewer.assigned_on_timestamp)
                if (reviewer.username != pull_request.author_username) and (
                    pending_review_time >=
                    datetime.timedelta(hours=max_waiting_time_in_hours)
                ):
                    reviewer_to_assigned_prs[reviewer.username].append(pull_request)
    return reviewer_to_assigned_prs


# Here we use type Any because the response we get from the api call is hard
# to annotate in a typedDict.
@check_token
def get_pull_request_object_from_dict(
    org_name: str,
    repo_name: str,
    pr_dict: Dict[str, Any]
) -> github_domain.PullRequest:
    """Fetch PR timelines and create Pull Request objects from response dictionary."""

    pr_number = pr_dict['number']
    activity_url = ISSUE_TIMELINE_URL_TEMPLATE.format(
        org_name, repo_name, pr_number)

    page_number = 1
    while True:
        logging.info('Fetching PR #%s timeline', pr_number)
        response = requests.get(
            activity_url,
            params={'page': page_number, 'per_page': 100},
            headers={
                'Accept': 'application/vnd.github+json',
                'Authorization': f'token {_TOKEN}'},
            timeout=TIMEOUT_SECS
        )
        response.raise_for_status()
        timeline_subset = response.json()

        if len(timeline_subset) == 0:
            break

        for event in timeline_subset:
            if event['event'] != 'assigned':
                continue
            updated_pr_dict = get_pull_request_dict_with_timestamp(pr_dict, event)

        page_number += 1

    return github_domain.PullRequest.from_github_response(
        updated_pr_dict)


# Here we use type Any because the response we get from the api call is hard
# to annotate in a typedDict.
def get_pull_request_dict_with_timestamp(
    pr_dict: Dict[str, Any],
    event: Dict[str, Any]
) -> Dict[str, Any]:
    """Adds the timestamp in dictionary as a key value pair where the key is `created_at`
    and the value is datetime when the reviewer was assigned.
    """

    for assignee in pr_dict['assignees']:
        if event['assignee']['login'] == assignee['login']:
            assignee['created_at'] = parser.parse(event['created_at'])
    return pr_dict


@check_token
def _get_discussion_data(
    org_name: str,
    repo_name: str,
    discussion_category: str,
    discussion_title: str,
) -> Tuple[str, int]:
    """Fetch discussion data from api and return corresponding discussion id and
    discussion number.
    """

    # The following query is written in GraphQL and is being used to fetch the category
    # ids and titles from the GitHub discussions. To learn more, check this out
    # https://docs.github.com/en/graphql.
    query = """
        query ($org_name: String!, $repository: String!) {
            repository(owner: $org_name, name: $repository) {
                discussionCategories(first: 10) {
                    nodes {
                        id
                        name
                    }
                }
            }
        }
    """

    variables = {
        'org_name': org_name,
        'repository': repo_name
    }

    response = requests.post(
        GITHUB_GRAPHQL_URL,
        json={'query': query, 'variables': variables},
        headers=_get_request_headers(),
        timeout=TIMEOUT_SECS
    )
    data = response.json()

    category_id = None
    discussion_categories = (
        data['data']['repository']['discussionCategories']['nodes'])

    for category in discussion_categories:
        if category['name'] == discussion_category:
            category_id = category['id']
            break

    if category_id is None:
        raise builtins.BaseException(
            f'{discussion_category} category is missing in GitHub Discussion.')

    # The following query is written in GraphQL and is being used to fetch discussions
    # from a particular GitHub discussion category. This helps to find out the discussion
    # where we want to comment. To learn more, check this out
    # https://docs.github.com/en/graphql.

    query = """
        query ($org_name: String!, $repository: String!, $category_id: ID!) {
            repository(owner: $org_name, name: $repository) {
                discussions(categoryId: $category_id, last:10) {
                    nodes {
                        id
                        title
                        number
                    }
                }
            }
        }
    """

    variables = {
        'org_name': org_name,
        'repository': repo_name,
        'category_id': category_id
    }

    response = requests.post(
        GITHUB_GRAPHQL_URL,
        json={'query': query, 'variables': variables},
        headers=_get_request_headers(),
        timeout=TIMEOUT_SECS
    )
    data = response.json()
    discussion_id = None

    discussions = data['data']['repository']['discussions']['nodes']

    for discussion in discussions:
        if discussion['title'] == discussion_title:
            discussion_id = discussion['id']
            discussion_number = discussion['number']
            break

    if discussion_id is None:
        raise builtins.BaseException(
            f'Discussion with title {discussion_title} not found, please create a '
            'discussion with that title.')

    return discussion_id, discussion_number


def _get_past_time(days: int=60) -> str:
    """Returns the subtraction of current time and the arg passed in days."""
    return (
        datetime.datetime.now(
            datetime.timezone.utc) - datetime.timedelta(days=days)).strftime(
            '%Y-%m-%dT%H:%M:%SZ')


def _get_old_comment_ids(
    org_name: str,
    repo_name: str,
    discussion_number: int
) -> List[str]:
    """Return the old comment ids."""

    # The following query is written in GraphQL and is being used to fetch the oldest 50
    # comments in a existing GitHub discussions. Assuming that, this workflow will run
    # twice every week, we will may not have more than 50 comments to delete. To learn
    # more, check this out https://docs.github.com/en/graphql.
    query = """
        query ($org_name: String!, $repository: String!, $discussion_number: Int!) {
            repository(owner: $org_name, name: $repository) {
                discussion(number: $discussion_number) {
                    comments(first: 50) {
                        nodes {
                            id
                            createdAt
                        }
                    }
                }
            }
        }
    """

    variables = {
        'org_name': org_name,
        'repository': repo_name,
        'discussion_number': discussion_number
    }

    response = requests.post(
        GITHUB_GRAPHQL_URL,
        json={'query': query, 'variables': variables},
        headers=_get_request_headers(),
        timeout=TIMEOUT_SECS
    )

    response.raise_for_status()
    data = response.json()

    comment_ids: List[str] = []

    discussion_comments = (
        data['data']['repository']['discussion']['comments']['nodes']
    )

    # Delete comments posted before this time.
    delete_comments_before_in_days = _get_past_time(DELETE_COMMENTS_BEFORE_IN_DAYS)

    for comment in discussion_comments:
        if comment['createdAt'] < delete_comments_before_in_days:
            comment_ids.append(comment['id'])
        else:
            break

    return comment_ids


def _delete_comment(comment_id: str) -> None:
    """Delete the GitHub Discussion comment related to the comment id."""

    query = """
        mutation deleteComment($comment_id: ID!) {
            deleteDiscussionComment(input: {id: $comment_id}) {
                clientMutationId
                comment {
                    bodyText
                }
            }
        }
    """

    variables = {
        'comment_id': comment_id
    }

    response = requests.post(
        GITHUB_GRAPHQL_URL,
        json={'query': query, 'variables': variables},
        headers=_get_request_headers(),
        timeout=TIMEOUT_SECS
    )
    response.raise_for_status()


def _post_comment(discussion_id: str, message: str) -> None:
    """Post the given message in an existing discussion."""

    # The following code is written in GraphQL and is being used to perform a mutation
    # operation. More specifically, we are using it to comment in GitHub discussion to
    # let reviewers know about some of their pending tasks. To learn more, check this out:
    # https://docs.github.com/en/graphql.
    query = """
        mutation post_comment($discussion_id: ID!, $comment: String!) {
            addDiscussionComment(input: {discussionId: $discussion_id, body: $comment}) {
                clientMutationId
                comment {
                    id
                }
            }
        }
    """

    variables = {
        'discussion_id': discussion_id,
        'comment': message
    }

    response = requests.post(
        GITHUB_GRAPHQL_URL,
        json={'query': query, 'variables': variables},
        headers=_get_request_headers(),
        timeout=TIMEOUT_SECS
    )
    response.raise_for_status()


@check_token
def delete_discussion_comments(
    org_name: str,
    repo_name: str,
    discussion_category: str,
    discussion_title: str
) -> None:
    """Delete old comments from GitHub Discussion."""

    _, discussion_number = _get_discussion_data(
        org_name, repo_name, discussion_category, discussion_title)

    comment_ids = _get_old_comment_ids(org_name, repo_name, discussion_number)

    for comment_id in comment_ids:
        _delete_comment(comment_id)


@check_token
def add_discussion_comments(
    org_name: str,
    repo_name: str,
    discussion_category: str,
    discussion_title: str,
    message: str
) -> None:
    """Add comments in an existing GitHub discussion."""

    discussion_id, _ = _get_discussion_data(
        org_name, repo_name, discussion_category, discussion_title)

    _post_comment(discussion_id, message)
