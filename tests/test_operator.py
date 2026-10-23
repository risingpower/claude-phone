from claude_phone.operator import match, spoken_extension
from claude_phone.registry import Session


def sess(ext, repo, branch="main", worktree=None):
    return Session(session_id=f"s{ext}", ext=ext, repo=repo, branch=branch, worktree=worktree, cwd=None,
                   iterm_id=None, status="idle", started_at=0, updated_at=0)


LIVE = [sess(301, "claude-phone", "master"), sess(302, "app-fe-v2"), sess(303, "site-be-v2"),
        sess(304, "lemon-stand", "feat/ags-2")]


def test_spoken_extension():
    assert spoken_extension("three oh one please") == 301
    assert spoken_extension("put me through to 303") == 303
    assert spoken_extension("the backend") is None


def test_backend_and_frontend_synonyms():
    assert match("The backend.", LIVE)[0].ext == 303
    assert match("the back end repo", LIVE)[0].ext == 303
    assert match("the frontend", LIVE)[0].ext == 302


def test_repo_names_spoken_naturally():
    assert match("claude phone.", LIVE)[0].ext == 301
    assert match("lemon stand", LIVE)[0].ext == 304


def test_spelled_out_branch():
    assert match("the a g s branch", LIVE)[0].ext == 304


def test_extension_wins():
    assert match("three oh two", LIVE)[0].ext == 302


def test_ambiguous_returns_candidates():
    two = [sess(305, "site-be-v2", "main"), sess(306, "site-be-v2", "staging")]
    hit, cands = match("site backend", two)
    assert hit is None and {c.ext for c in cands} == {305, 306}
    assert match("site backend staging", two)[0].ext == 306


def test_nothing_matches():
    assert match("the weather forecast", LIVE) == (None, [])


def test_back_alone_is_not_the_backend():
    assert match("Can you turn them back on again?", LIVE)[0] is None
    assert match("the back end", LIVE)[0].ext == 303
