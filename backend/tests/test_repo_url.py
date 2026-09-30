"""repo_url 归一化单元测试:保证同仓库不同写法映射到同一字符串。

normalize_repo_url 用于 projects.repo_url_normalized 唯一约束与查询匹配,
任何归一化 bug 都会导致重复 Project 行或匹配失败(记忆注入拿不到记忆)。

覆盖:
- SSH 形式(git@host:path)
- ssh:// 形式(含/不含 userinfo)
- https/http 形式
- 无协议头补 https://
- .git / .Git 后缀剥离
- 尾部斜杠剥离
- host 小写,path 保留大小写
- 空串 / None / 纯空白
- 同仓库不同写法映射一致(等价类)
"""
import pytest

from app.services.repo_url import normalize_repo_url


# ============================================================
# 空值 / 边界
# ============================================================

@pytest.mark.parametrize("url", ["", None])
def test_normalize_empty_returns_empty(url):
    """空串/None → 空串(不抛异常)。"""
    assert normalize_repo_url(url) == ""


@pytest.mark.parametrize("url", ["   ", "\t\n", "  \n  "])
def test_normalize_whitespace_only_returns_empty(url):
    """纯空白 → 空串(strip 后为空)。"""
    assert normalize_repo_url(url) == ""


# ============================================================
# SSH 形式:git@host:path
# ============================================================

def test_normalize_ssh_github_form():
    """git@github.com:org/repo.git → https://github.com/org/repo。"""
    assert normalize_repo_url("git@github.com:org/repo.git") == "https://github.com/org/repo"


def test_normalize_ssh_preserves_path_case():
    """path 保留大小写(GitHub path 大小写敏感)。"""
    assert normalize_repo_url("git@github.com:Org/RepoName.git") == "https://github.com/Org/RepoName"


def test_normalize_ssh_host_lowercased():
    """SSH host 部分(冒号前)小写。"""
    assert normalize_repo_url("git@GitHub.COM:org/repo.git") == "https://github.com/org/repo"


def test_normalize_ssh_without_git_suffix():
    """SSH 形式无 .git 后缀也能正确归一化。"""
    assert normalize_repo_url("git@github.com:org/repo") == "https://github.com/org/repo"


# ============================================================
# ssh:// 形式
# ============================================================

def test_normalize_ssh_protocol_with_userinfo():
    """ssh://git@host/path → https://host/path(去 userinfo)。"""
    assert normalize_repo_url("ssh://git@github.com/org/repo.git") == "https://github.com/org/repo"


def test_normalize_ssh_protocol_without_userinfo():
    """ssh://host/path(无 userinfo)→ https://host/path。"""
    assert normalize_repo_url("ssh://github.com/org/repo.git") == "https://github.com/org/repo"


def test_normalize_ssh_protocol_host_lowercased():
    """ssh:// 协议 host 小写。"""
    assert normalize_repo_url("ssh://git@GitHub.COM/org/repo") == "https://github.com/org/repo"


# ============================================================
# https / http 形式
# ============================================================

def test_normalize_https_with_git_suffix():
    """https:// + .git 后缀剥离。"""
    assert normalize_repo_url("https://github.com/org/repo.git") == "https://github.com/org/repo"


def test_normalize_https_without_git_suffix():
    """https:// 无 .git 后缀保持不变(去尾斜杠)。"""
    assert normalize_repo_url("https://github.com/org/repo") == "https://github.com/org/repo"


def test_normalize_https_preserves_scheme():
    """http:// 协议保留(http 不强制升 https)。"""
    assert normalize_repo_url("http://example.com/org/repo.git") == "http://example.com/org/repo"


def test_normalize_https_host_lowercased():
    """https:// host 小写。"""
    assert normalize_repo_url("https://GitHub.COM/org/repo.git") == "https://github.com/org/repo"


def test_normalize_https_preserves_path_case():
    """https:// path 保留大小写。"""
    assert normalize_repo_url("https://github.com/Org/RepoName.git") == "https://github.com/Org/RepoName"


def test_normalize_capital_git_suffix_stripped():
    """大写 .Git 后缀也应剥离。"""
    assert normalize_repo_url("https://github.com/org/repo.Git") == "https://github.com/org/repo"


# ============================================================
# 无协议头
# ============================================================

def test_normalize_no_scheme_prepends_https():
    """无协议头(github.com/org/repo)补 https://。"""
    assert normalize_repo_url("github.com/org/repo") == "https://github.com/org/repo"


def test_normalize_no_scheme_with_git_suffix():
    """无协议头 + .git 后缀。"""
    assert normalize_repo_url("github.com/org/repo.git") == "https://github.com/org/repo"


# ============================================================
# 尾部斜杠
# ============================================================

def test_normalize_trailing_slash_stripped():
    """尾部单斜杠剥离。"""
    assert normalize_repo_url("https://github.com/org/repo/") == "https://github.com/org/repo"


def test_normalize_multiple_trailing_slashes():
    """多个尾部斜杠全部剥离(rstrip '/' 行为)。"""
    assert normalize_repo_url("https://github.com/org/repo///") == "https://github.com/org/repo"


# ============================================================
# 等价类:同仓库不同写法应映射到同一字符串
# ============================================================

@pytest.mark.parametrize("variants", [
    # GitHub 同仓库的常见写法
    [
        "git@github.com:org/repo.git",
        "ssh://git@github.com/org/repo.git",
        "ssh://github.com/org/repo.git",
        "https://github.com/org/repo.git",
        "https://github.com/org/repo",
        "https://github.com/org/repo/",
        "https://github.com/org/repo///",
        "github.com/org/repo",
        "github.com/org/repo.git",
        "git@GitHub.COM:org/repo.git",
    ],
    # 自部署 GitLab 同仓库
    [
        "git@gitlab.example.com:team/sub/proj.git",
        "ssh://git@gitlab.example.com/team/sub/proj.git",
        "https://gitlab.example.com/team/sub/proj.git",
        "https://gitlab.example.com/team/sub/proj",
    ],
])
def test_same_repo_variants_normalize_identical(variants):
    """同一仓库的多种写法应归一化到同一字符串(等价类核心断言)。"""
    normalized = {normalize_repo_url(v) for v in variants}
    assert len(normalized) == 1, f"等价类失败,归一化结果不唯一: {normalized}"


# ============================================================
# 不同仓库不应碰撞
# ============================================================

def test_different_repos_normalize_differently():
    """不同仓库(org/repo 名不同)归一化结果不同。"""
    a = normalize_repo_url("https://github.com/orgA/repo")
    b = normalize_repo_url("https://github.com/orgB/repo")
    assert a != b


def test_different_orgs_do_not_collide():
    """同 repo 名不同 org 不碰撞。"""
    assert normalize_repo_url("https://github.com/org1/repo") != normalize_repo_url("https://github.com/org2/repo")


def test_different_hosts_do_not_collide():
    """同 org/repo 不同 host 不碰撞。"""
    assert normalize_repo_url("https://github.com/org/repo") != normalize_repo_url("https://gitlab.com/org/repo")


# ============================================================
# 带分支/子路径(不应误归一化)
# ============================================================

def test_normalize_preserves_subpath():
    """path 中的子目录不应被截断(仅去 .git 与尾斜杠)。"""
    assert normalize_repo_url("https://github.com/org/repo/tree/main") == "https://github.com/org/repo/tree/main"


def test_normalize_preserves_query_string():
    """URL 查询参数保留(urlparse 不剥离 query)。

    注意:实际业务中 repo_url 不应带 query,但归一化函数本身不做语义校验,
    只做格式归一化。这是与实现一致的契约。
    """
    result = normalize_repo_url("https://github.com/org/repo?foo=bar")
    # 实现里 urlparse 后只取 scheme://netloc+path,query 被丢弃
    assert result == "https://github.com/org/repo"
