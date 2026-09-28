import subprocess

import pytest

from gateway.plugins.git_push import GitPushPlugin


@pytest.fixture
def allowed_repo(tmp_path):
    repo = tmp_path / "allowed" / "mission-repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


@pytest.fixture
def outside_repo(tmp_path):
    repo = tmp_path / "outside" / "not-a-mission-repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def test_push_refused_when_repo_path_not_in_any_allowed_root(outside_repo):
    plugin = GitPushPlugin(allowed_roots=())
    result = plugin.execute({"repo_path": str(outside_repo), "branch": "main"})
    assert result.ok is False
    assert "not inside any allow-listed root" in result.detail


def test_push_refused_when_outside_the_one_configured_root(tmp_path, allowed_repo, outside_repo):
    plugin = GitPushPlugin(allowed_roots=(str(allowed_repo.parent),))
    result = plugin.execute({"repo_path": str(outside_repo), "branch": "main"})
    assert result.ok is False
    assert "not inside any allow-listed root" in result.detail


def test_path_traversal_via_dotdot_is_blocked(tmp_path, allowed_repo, outside_repo):
    """A repo_path that string-startswith the allowed root but escapes it
    via "../" must still be blocked - this is what resolving the path
    (Path.resolve()) before the containment check actually buys over a
    naive str.startswith() comparison."""
    plugin = GitPushPlugin(allowed_roots=(str(allowed_repo.parent),))
    traversal_path = str(allowed_repo) + "/../../outside/not-a-mission-repo"
    result = plugin.execute({"repo_path": traversal_path, "branch": "main"})
    assert result.ok is False
    assert "not inside any allow-listed root" in result.detail


def test_push_proceeds_past_the_allowlist_check_for_a_real_allowed_path(allowed_repo):
    # No real remote configured, so the actual `git push` will fail - but
    # what matters here is that it got PAST the allow-list refusal (a
    # different failure mode) and attempted a real git invocation.
    plugin = GitPushPlugin(allowed_roots=(str(allowed_repo.parent),))
    result = plugin.execute({"repo_path": str(allowed_repo), "branch": "main"})
    assert "not inside any allow-listed root" not in result.detail
    assert result.ok is False  # no remote named 'origin' configured
    assert "git push exited" in result.detail
