"""repo_url 归一化工具

保证同仓库不同写法(git@/ssh/https/.git/尾部斜杠/host 大小写)映射到同一字符串,
用于 projects 表的 repo_url_normalized 唯一约束与查询匹配。

归一化规则:
- git@github.com:org/repo.git → https://github.com/org/repo
- ssh://git@github.com/org/repo.git → https://github.com/org/repo
- https://github.com/org/repo.git → https://github.com/org/repo
- 去尾部 .git / .Git
- host 小写,path 保留大小写(GitHub path 大小写敏感)
- 无协议头补 https://
- 去尾部斜杠
"""
import re
from urllib.parse import urlparse


def normalize_repo_url(url: str) -> str:
    """归一化 repo_url,保证同仓库不同写法映射到同一行 Project。

    空串/None 返回空串。入库前统一调用本函数;注入/查询时也归一化匹配。
    旧 Task 数据不改(注入时现归一化查询即可)。
    """
    if not url:
        return ""
    url = url.strip()
    if not url:
        return ""

    # SSH 形式 git@host:path → https://host/path
    m = re.match(r"^git@([^:]+):(.+)$", url)
    if m:
        url = f"https://{m.group(1).lower()}/{m.group(2)}"
    elif url.startswith("ssh://"):
        # ssh://[git@]host/path → https://host/path
        rest = url[len("ssh://"):]
        host_part, _, path_part = rest.partition("/")
        # 去 userinfo (git@)
        if "@" in host_part:
            host_part = host_part.split("@", 1)[1]
        url = (
            f"https://{host_part.lower()}/{path_part}"
            if path_part else f"https://{host_part.lower()}"
        )
    elif not url.startswith(("http://", "https://")):
        url = "https://" + url

    # 统一 host 小写
    p = urlparse(url)
    url = f"{p.scheme}://{p.netloc.lower()}{p.path}"

    # 去 .git 后缀(常见 .git/.Git)
    if url.endswith(".git"):
        url = url[:-4]
    elif url.endswith(".Git"):
        url = url[:-4]

    return url.rstrip("/")
