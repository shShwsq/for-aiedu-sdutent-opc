"""用户上传在工作区中的路径布局(纯函数,零 app 依赖)

沙箱拷贝侧(app/tools/sandbox_tools.py 的 transfer_* / add_uploads_to_workspace)
与回退浏览侧(app/routers/workspace.py 的 uploads/tree / uploads/file)共用同一
布局约定,保证沙箱存活期看到的文件树与沙箱过期后的回退树路径一致:

- 创建上传仅 1 个 → 文件直接平铺工作根(repo_path = uploaded_files/,无子目录)
- 创建上传 ≥2 个 → 工作根下各占 {i}-{清洗文件名}/ 子目录(i 从 0 起)
- 追问上传     → followup_uploads/{i}-{name}/(i 为 params.followup_upload_ids
  累积列表的全局下标;传输/重放/回退均按全量列表编号,顺序稳定)

safe_dirname / SKIP_DIRS_LIST 由 sandbox_tools 迁移至此作为单一来源:
前者供沙箱拷贝与回退布局共用(名字不一致则回退路径全错),后者让上传回退树
与沙箱树共用同一噪声目录剪枝口径。
"""
from collections import namedtuple

# 噪声目录:列出文件树时跳过(参考 Claude Code LS 的 ignore 设计)
# 由 sandbox_tools._SKIP_DIRS_LIST 迁移至此,沙箱树与上传回退树共用
SKIP_DIRS_LIST = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt", "target",
}


def safe_dirname(name: str, fallback: str = "upload") -> str:
    """把任意文件名清洗为安全的单层目录名(追问多文件防碰撞用)

    只保留字母数字与 . _ -,其余换为 _;剔除 .. / 前后缀 dot;限长 80。
    """
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = base.replace("..", "_")
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "_" for c in base)
    cleaned = cleaned.strip("._")
    return (cleaned or fallback)[:80]


def extract_creation_ids(params: dict | None) -> list[str]:
    """创建时上传的 upload_id 列表(合并 legacy 单数 upload_id + upload_ids,去重保序)

    旧任务只写单数 upload_id;新任务写 upload_ids 列表。统一读取,供
    user_intent 提示 / orchestrator 分发传输 / 回退浏览布局计算复用。
    """
    p = params or {}
    ids: list[str] = []
    if p.get("upload_id"):
        ids.append(p["upload_id"])
    for x in (p.get("upload_ids") or []):
        if x:
            ids.append(x)
    seen: set[str] = set()
    out: list[str] = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def extract_followup_ids(params: dict | None) -> list[str]:
    """追问累积上传的 upload_id 列表(读 params.followup_upload_ids)

    沙箱回收后重放与工作区回退浏览都按此全量列表重放/重建
    (followup_uploads/{i}-{name} 的 i 即列表下标,顺序即追加顺序)。
    """
    return [x for x in ((params or {}).get("followup_upload_ids") or []) if x]


# 上传槽位:upload_id + 该上传内容在工作根下的落点前缀("" 表示平铺根)
UploadSlot = namedtuple("UploadSlot", ["upload_id", "prefix"])


def _slot_name(uid: str, meta: dict | None) -> str:
    """槽位目录名:meta.filename 清洗;meta 缺失(被 GC 等)回退 uid 前缀

    与 sandbox_tools 传输侧的命名逻辑完全一致(名字不同则路径对不上)。
    """
    return safe_dirname((meta or {}).get("filename") or "", fallback=uid[:12] or "upload")


def compute_upload_layout(
    creation_ids: list[str],
    followup_ids: list[str],
    metas: dict[str, dict | None],
) -> list[UploadSlot]:
    """计算各上传在工作根下的落点前缀(与沙箱拷贝布局一一对应)

    metas: {upload_id: meta dict};缺失(GC/损坏)传 {} 或 None,目录名
    回退 uid 前缀,与传输侧 meta 加载失败时的命名一致。
    """
    slots: list[UploadSlot] = []
    single = len(creation_ids) == 1
    for i, uid in enumerate(creation_ids):
        # 单上传平铺根(transfer_upload_to_workspace);多上传各占 {i}-{name}/
        prefix = "" if single else f"{i}-{_slot_name(uid, metas.get(uid))}"
        slots.append(UploadSlot(uid, prefix))
    for i, uid in enumerate(followup_ids):
        slots.append(UploadSlot(uid, f"followup_uploads/{i}-{_slot_name(uid, metas.get(uid))}"))
    return slots


def resolve_path(slots: list[UploadSlot], path: str) -> tuple[str, str] | None:
    """把工作根相对路径反解为 (upload_id, files/ 内相对路径);无匹配返回 None

    多前缀歧义时取最长前缀优先:如平铺根上传的 zip 内恰好含
    followup_uploads/ 目录时,真实追问槽位(前缀更长)优先命中。
    """
    p = (path or "").replace("\\", "/").strip("/")
    if not p:
        return None
    best: tuple[int, str, str] | None = None  # (prefix_len, upload_id, relpath)
    for slot in slots:
        if not slot.prefix:
            rel = p
        elif p.startswith(slot.prefix + "/"):
            rel = p[len(slot.prefix) + 1:]
        else:
            continue
        if not rel:
            continue  # 恰好是槽位目录本身(不可作为文件读)
        if best is None or len(slot.prefix) > best[0]:
            best = (len(slot.prefix), slot.upload_id, rel)
    if best is None:
        return None
    return (best[1], best[2])
