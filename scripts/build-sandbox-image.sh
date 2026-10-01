#!/usr/bin/env bash
# 构建 SecondLook 沙箱镜像(预装 git / ripgrep / python3 / awk / find,Semgrep 默认预装可 --no-semgrep 省略)
#
# 默认同时预装三款 CLI 执行器:
#   - Qoder CLI(国际版):Node.js + @qoder-ai/qodercli,需 qoder.com 账号
#   - DeepSeek Harness CLI(开源):Node.js + @deepseek-ai/dsh,需 DeepSeek API Key
#   - Codex CLI(OpenAI 官方,开源):Node.js + @openai/codex,需 LLM API Key(支持自定义端点)
#
# 支持 task.executor=qoder_cli / deepseek_cli / codex_cli。
#
# Node 版本策略:qodercli 要求 >= 20.0.0,dsh 要求 >= 20,codex 要求 >= 16。
# 只要任一 Node 类 CLI 启用(qoder_cli / deepseek_cli / codex_cli),统一装 Node 22.x(三者都兼容)。
#
# 用法:在装好 docker 的 Linux 服务器上执行
#   bash scripts/build-sandbox-image.sh                                       # 默认装三款 CLI
#   bash scripts/build-sandbox-image.sh --no-qoder-cli                        # 不装 qoder
#   bash scripts/build-sandbox-image.sh --no-deepseek-cli                     # 不装 dsh
#   bash scripts/build-sandbox-image.sh --no-codex-cli                        # 不装 codex
#   bash scripts/build-sandbox-image.sh --no-qoder-cli --no-deepseek-cli --no-codex-cli  # 仅基础工具
#   bash scripts/build-sandbox-image.sh --no-semgrep                          # 不预装 Semgrep
#
# 国内镜像加速(服务器在国内时推荐,避免 docker.io 拉取超时):
#   bash scripts/build-sandbox-image.sh --cn-mirror                           # 一键国内源(Docker+apt+npm)
#   bash scripts/build-sandbox-image.sh --registry docker.m.daocloud.io       # 仅换 Docker 基础镜像源
#
# 构建 secondlook-sandbox:latest 后,在 SecondLook backend/.env 设:
#   SANDBOX_IMAGE=secondlook-sandbox:latest

set -euo pipefail

IMAGE_NAME="secondlook-sandbox"
IMAGE_TAG="latest"
DOCKERFILE="Dockerfile.sandbox"

# ---------- 参数解析 ----------
# 三款 CLI 独立开关,默认都装
# - 国际版 qodercli:需 Node.js + npm(镜像体积较大,约 +200MB)
# - DeepSeek Harness CLI(dsh):需 Node.js + npm(与 Node 类 CLI 共享 Node 22.x 运行时)
# - Codex CLI:Node.js + npm(OpenAI 官方,与 Node 类 CLI 共享 Node 22.x 运行时)
WITH_QODER_CLI=1
WITH_DEEPSEEK_CLI=1
WITH_CODEX_CLI=1
# Semgrep(内置 react_agent 的 run_semgrep 工具依赖),默认也装,可用 --no-semgrep 省略
WITH_SEMGREP=1
# 镜像源(国内加速),默认空 = 用官方源
# - REGISTRY:Docker 基础镜像源前缀,如 docker.m.daocloud.io(非空时 FROM $REGISTRY/ubuntu:24.04)
# - APT_MIRROR:apt 源,目前支持 aliyun(空 = 不换)
# - NPM_MIRROR:npm 源,目前支持 npmmirror(空 = 不换)
REGISTRY=""
APT_MIRROR=""
NPM_MIRROR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --with-qoder-cli)
            WITH_QODER_CLI=1
            shift
            ;;
        --no-qoder-cli)
            WITH_QODER_CLI=0
            shift
            ;;
        --with-deepseek-cli)
            WITH_DEEPSEEK_CLI=1
            shift
            ;;
        --no-deepseek-cli)
            WITH_DEEPSEEK_CLI=0
            shift
            ;;
        --with-codex-cli)
            WITH_CODEX_CLI=1
            shift
            ;;
        --no-codex-cli)
            WITH_CODEX_CLI=0
            shift
            ;;
        --with-semgrep)
            WITH_SEMGREP=1
            shift
            ;;
        --no-semgrep)
            WITH_SEMGREP=0
            shift
            ;;
        --registry)
            # 下一个参数为镜像源前缀
            if [ $# -lt 2 ]; then
                echo "[FAIL] --registry 需要一个参数(如 --registry docker.m.daocloud.io)"
                exit 1
            fi
            REGISTRY="$2"
            shift 2
            ;;
        --cn-mirror)
            # 一键国内加速:Docker 用 DaoCloud 镜像 + apt 阿里云 + npm npmmirror
            REGISTRY="docker.m.daocloud.io"
            APT_MIRROR="aliyun"
            NPM_MIRROR="npmmirror"
            shift
            ;;
        -h|--help)
            echo "用法:bash $0 [选项](均可组合,默认全部启用)"
            echo ""
            echo "CLI 开关(默认全装):"
            echo "  --with-qoder-cli       预装 Qoder CLI 国际版(Node.js + npm,需 qoder.com 账号)"
            echo "  --no-qoder-cli         不装 qoder"
            echo "  --with-deepseek-cli    预装 DeepSeek Harness CLI dsh(Node.js + npm,需 DeepSeek API Key)"
            echo "  --no-deepseek-cli      不装 dsh"
            echo "  --with-codex-cli       预装 Codex CLI(Node.js + npm,OpenAI 官方,需 LLM API Key)"
            echo "  --no-codex-cli         不装 codex"
            echo "  --with-semgrep         预装 Semgrep(内置 react_agent 的 run_semgrep 工具用,默认装)"
            echo "  --no-semgrep           不装 semgrep(不预装时 run_semgrep 首次运行会兜底自动安装,但耗时长)"
            echo ""
            echo "镜像源(服务器在国内时推荐,避免 docker.io 拉取超时):"
            echo "  --cn-mirror            一键国内加速(Docker DaoCloud + apt 阿里云 + npm npmmirror)"
            echo "  --registry <prefix>    仅换 Docker 基础镜像源前缀(如 docker.m.daocloud.io)"
            echo "                         非空时 FROM <prefix>/ubuntu:24.04;阿里云需带 library/ 前缀"
            echo ""
            echo "基础工具(git/rg/python3/awk/find/curl)始终预装。"
            echo "Node.js 版本:只要 qoder_cli / deepseek_cli / codex_cli 任一启用,统一装 Node 22.x(三者都兼容)。"
            exit 0
            ;;
        *)
            echo "[FAIL] 未知参数: $1(用 -h 查看帮助)"
            exit 1
            ;;
    esac
done

# ---------- 前置检查 ----------
if ! command -v docker >/dev/null 2>&1; then
    echo "[FAIL] docker 未安装或未运行,请先安装 Docker Engine 20.10+"
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "[FAIL] docker daemon 不可用(可能当前用户不在 docker 组,或 docker 未启动)"
    echo "       解决:sudo usermod -aG docker \$USER 然后重登,或 sudo systemctl start docker"
    exit 1
fi

# ---------- 是否需要装 Node.js ----------
# qodercli 要求 >= 20.0.0,dsh 要求 >= 20,codex 要求 >= 16,统一用 Node 22.x(三者都兼容)
NEED_NODE=0
if [ "$WITH_QODER_CLI" -eq 1 ] || [ "$WITH_DEEPSEEK_CLI" -eq 1 ] || [ "$WITH_CODEX_CLI" -eq 1 ]; then
    NEED_NODE=1
fi

# ---------- 基础镜像源 ----------
# REGISTRY 非空时用 $REGISTRY/ubuntu:24.04,否则用官方 ubuntu:24.04
if [ -n "$REGISTRY" ]; then
    BASE_IMAGE="$REGISTRY/ubuntu:24.04"
else
    BASE_IMAGE="ubuntu:24.04"
fi
# registry marker(检测配置变更用),空 = default
expect_registry_marker="${REGISTRY:-default}"

# ---------- 生成 Dockerfile(若不存在或配置不符) ----------
# 检测:Dockerfile 现状与本次期望的 CLI 组合是否一致,不一致则备份重生成。
# 不用 sed 追加(反引号/转义/位置错乱等问题太多),直接按期望状态覆盖最可靠。
#
# 期望标记(在 Dockerfile 中以注释形式存在,便于检测):
#   # @qoder-cli:yes / # @qoder-cli:no          国际版状态
#   # @deepseek-cli:yes / # @deepseek-cli:no    dsh 状态
#   # @codex-cli:yes / # @codex-cli:no          codex 状态
#   # @semgrep:yes / # @semgrep:no              semgrep 状态
NEED_REGEN=0
REGEN_REASON=""
expect_qoder_cli_marker=$([ "$WITH_QODER_CLI" -eq 1 ] && echo "yes" || echo "no")
expect_deepseek_cli_marker=$([ "$WITH_DEEPSEEK_CLI" -eq 1 ] && echo "yes" || echo "no")
expect_codex_cli_marker=$([ "$WITH_CODEX_CLI" -eq 1 ] && echo "yes" || echo "no")
expect_semgrep_marker=$([ "$WITH_SEMGREP" -eq 1 ] && echo "yes" || echo "no")

if [ ! -f "$DOCKERFILE" ]; then
    NEED_REGEN=1
    REGEN_REASON="文件不存在,全新生成"
else
    cur_qoder_cli=$(grep -E "^# @qoder-cli:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_deepseek_cli=$(grep -E "^# @deepseek-cli:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_codex_cli=$(grep -E "^# @codex-cli:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_semgrep=$(grep -E "^# @semgrep:" "$DOCKERFILE" | head -1 | sed 's/.*://' || echo "")
    cur_registry=$(grep -E "^# @registry:" "$DOCKERFILE" | head -1 | sed 's/^# @registry://' || echo "")
    if [ "$cur_registry" != "$expect_registry_marker" ]; then
        NEED_REGEN=1
        REGEN_REASON="镜像源变更($cur_registry → $expect_registry_marker)"
    elif [ "$cur_deepseek_cli" != "$expect_deepseek_cli_marker" ]; then
        # 旧版 Dockerfile 无 @deepseek-cli marker(cur_deepseek_cli 为空)也会命中这里,自动重生成
        NEED_REGEN=1
        REGEN_REASON="DeepSeek CLI 配置变更(${cur_deepseek_cli:-无标记} → $expect_deepseek_cli_marker)"
    elif [ "$cur_qoder_cli" != "$expect_qoder_cli_marker" ]; then
        NEED_REGEN=1
        REGEN_REASON="国际版配置变更($cur_qoder_cli → $expect_qoder_cli_marker)"
    elif [ "$cur_codex_cli" != "$expect_codex_cli_marker" ]; then
        NEED_REGEN=1
        REGEN_REASON="Codex 配置变更($cur_codex_cli → $expect_codex_cli_marker)"
    elif [ "$cur_semgrep" != "$expect_semgrep_marker" ]; then
        # 旧版 Dockerfile 无 @semgrep marker(cur_semgrep 为空)也会命中这里,自动重生成补上/去掉 semgrep
        NEED_REGEN=1
        REGEN_REASON="Semgrep 配置变更(${cur_semgrep:-无标记} → $expect_semgrep_marker)"
    elif grep -q '\\n' "$DOCKERFILE" 2>/dev/null; then
        # 旧版生成的 RUN 续行用 printf '\\n' 输出了字面 \n(sh 报 bad variable name),兜底重生成
        NEED_REGEN=1
        REGEN_REASON="检测到 RUN 续行 bug(字面 \\n),需重新生成"
    elif [ "$WITH_QODER_CLI" -eq 1 ] && ! grep -q "qodercli" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含国际版但缺 qodercli 安装行,需重新生成"
    elif [ "$WITH_DEEPSEEK_CLI" -eq 1 ] && ! grep -q "@deepseek-ai/dsh" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含 dsh 但缺 @deepseek-ai/dsh 安装行,需重新生成"
    elif [ "$WITH_CODEX_CLI" -eq 1 ] && ! grep -q "@openai/codex" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含 Codex 但缺 @openai/codex 安装行,需重新生成"
    elif [ "$WITH_SEMGREP" -eq 1 ] && ! grep -q "pip install.*semgrep" "$DOCKERFILE"; then
        NEED_REGEN=1
        REGEN_REASON="标记为含 Semgrep 但缺 semgrep 安装行,需重新生成"
    fi
fi

# 已存在且需要重新生成 → 备份原文件(不丢用户自定义内容)
if [ "$NEED_REGEN" -eq 1 ] && [ -f "$DOCKERFILE" ]; then
    BACKUP="${DOCKERFILE}.bak"
    cp "$DOCKERFILE" "$BACKUP"
    echo "[INFO] $DOCKERFILE 已存在,$REGEN_REASON"
    echo "       原文件备份到 $BACKUP(含用户自定义内容,可手动合并回新 Dockerfile)"
fi

if [ "$NEED_REGEN" -eq 1 ]; then
    # ---- 生成 Dockerfile:基础部分 ----
    # 基础工具始终含 curl(NodeSource 安装脚本需要)
    cat > "$DOCKERFILE" <<'EOF'
# @qoder-cli:__QODER_CLI_MARKER__
# @deepseek-cli:__DEEPSEEK_CLI_MARKER__
# @codex-cli:__CODEX_CLI_MARKER__
# @semgrep:__SEMGREP_MARKER__
# @registry:__REGISTRY_MARKER__
FROM __BASE_IMAGE__

# 避免 tzdata 等交互式安装卡住
ENV DEBIAN_FRONTEND=noninteractive
EOF

    # ---- 国内 apt 源(可选,--cn-mirror 时启用)----
    if [ "$APT_MIRROR" = "aliyun" ]; then
        cat >> "$DOCKERFILE" <<'EOF'
# 国内 apt 源加速(阿里云;ubuntu 24.04 DEB822 格式 + 旧 sources.list 兼容)
RUN sed -i 's@//.*archive.ubuntu.com@//mirrors.aliyun.com@g; s@//.*security.ubuntu.com@//mirrors.aliyun.com@g' \
        /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true \
    && sed -i 's@//.*archive.ubuntu.com@//mirrors.aliyun.com@g; s@//.*security.ubuntu.com@//mirrors.aliyun.com@g' \
        /etc/apt/sources.list 2>/dev/null || true
EOF
    fi

    # ---- 基础工具 ----
    cat >> "$DOCKERFILE" <<'EOF'
# 基础工具:git / ripgrep / python3 / awk / find / curl
# curl 用于:NodeSource 安装脚本(Node 类 CLI 的 Node.js 安装)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ripgrep \
        python3 \
        python3-pip \
        ca-certificates \
        openssh-client \
        coreutils \
        findutils \
        gawk \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python
EOF

    # ---- 追加 Semgrep(内置 react_agent 的 run_semgrep 工具依赖,默认装,--no-semgrep 可省略)----
    if [ "$WITH_SEMGREP" -eq 1 ]; then
        cat >> "$DOCKERFILE" <<'EOF'

# ---- Semgrep(内置 react_agent 的 run_semgrep 工具依赖)----
# 预装进镜像,避免首次任务运行时 pip install(慢 + 非 root 用户撞 PEP 668)。
# 未预装时 run_semgrep 也会兜底自动安装(--user --break-system-packages),但首次耗时长。
RUN pip install --no-cache-dir --break-system-packages semgrep \
    && semgrep --version
EOF
    fi

    # ---- 追加 Node.js(若任一 Node 类 CLI 启用)----
    # 统一装 Node 22.x:qodercli 要求 >= 20.0.0,dsh 要求 >= 20,codex 要求 >= 16,22.x 三者都兼容
    if [ "$NEED_NODE" -eq 1 ]; then
        if [ "$NPM_MIRROR" = "npmmirror" ]; then
            cat >> "$DOCKERFILE" <<'EOF'

# ---- Node.js 22.x(qodercli >= 20.0.0 / dsh >= 20 / codex >= 16,统一用 22.x)----
# npm 全局源换成 npmmirror(国内加速 qodercli/dsh/codex 的 npm install -g)
USER root
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm config set registry https://registry.npmmirror.com
EOF
        else
            cat >> "$DOCKERFILE" <<'EOF'

# ---- Node.js 22.x(qodercli >= 20.0.0 / dsh >= 20 / codex >= 16,统一用 22.x)----
USER root
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
EOF
        fi
    fi

    # ---- 追加国际版 Qoder CLI ----
    if [ "$WITH_QODER_CLI" -eq 1 ]; then
        cat >> "$DOCKERFILE" <<'EOF'

# ---- Qoder CLI 国际版(qodercli,官方 npm 包 @qoder-ai/qodercli)----
# 见 https://docs.qoder.com/cli/install
# Node.js 已由上面的 setup_22.x 安装(qodercli 兼容 Node 22)
USER root
RUN npm install -g @qoder-ai/qodercli
EOF
    fi

    # ---- 追加 DeepSeek Harness CLI(dsh)----
    if [ "$WITH_DEEPSEEK_CLI" -eq 1 ]; then
        cat >> "$DOCKERFILE" <<'EOF'

# ---- DeepSeek Harness CLI(dsh,开源 https://github.com/deepseek-ai/deepseek-harness)----
# 官方 npm 包 @deepseek-ai/dsh,bin 名 dsh(dsh --profile acp 启动随附的 stdio ACP 服务)
# acp profile 首次使用时自动从模板初始化;凭证经 DEEPSEEK_API_KEY 环境变量注入
# Node.js 已由上面的 setup_22.x 安装(dsh 要求 >= 20)
USER root
RUN npm install -g @deepseek-ai/dsh \
    && dsh --version
EOF
    fi

    # ---- 追加 Codex CLI(OpenAI 官方,npm 安装)----
    if [ "$WITH_CODEX_CLI" -eq 1 ]; then
        cat >> "$DOCKERFILE" <<'EOF'

# ---- Codex CLI(OpenAI 官方,开源 https://github.com/openai/codex)----
# 官方 npm 包 @openai/codex,bin 名 codex(codex exec --json 非交互模式)
# 不原生支持 ACP,通过 codex_bridge.py 翻译 codex exec --json JSONL → ACP
# Node.js 已由上面的 setup_22.x 安装(codex 要求 >= 16,Node 22 兼容)
USER root
RUN npm install -g @openai/codex \
    && codex --version
EOF
    fi

    # ---- 追加非 root 用户 ----
    cat >> "$DOCKERFILE" <<'EOF'

# 沙箱默认非 root 用户 user,确保 home 目录存在
RUN useradd -m -s /bin/bash user
USER user
WORKDIR /home/user
EOF

    # 替换标记占位符为实际值(BASE_IMAGE 含 /,用 # 作 sed 分隔符)
    sed -i \
        -e "s/__QODER_CLI_MARKER__/$expect_qoder_cli_marker/" \
        -e "s/__DEEPSEEK_CLI_MARKER__/$expect_deepseek_cli_marker/" \
        -e "s/__CODEX_CLI_MARKER__/$expect_codex_cli_marker/" \
        -e "s/__SEMGREP_MARKER__/$expect_semgrep_marker/" \
        -e "s/__REGISTRY_MARKER__/$expect_registry_marker/" \
        -e "s#__BASE_IMAGE__#$BASE_IMAGE#" \
        "$DOCKERFILE"

    echo "[OK] 已生成 $DOCKERFILE($REGEN_REASON)"
    echo "     国际版(qodercli):$([ "$WITH_QODER_CLI" -eq 1 ] && echo '装' || echo '不装')"
    echo "     DeepSeek(dsh):$([ "$WITH_DEEPSEEK_CLI" -eq 1 ] && echo '装' || echo '不装')"
    echo "     Codex(codex):$([ "$WITH_CODEX_CLI" -eq 1 ] && echo '装' || echo '不装')"
    echo "     Semgrep(semgrep):$([ "$WITH_SEMGREP" -eq 1 ] && echo '装' || echo '不装')"
    echo "     镜像源:${REGISTRY:-默认(docker.io)}${APT_MIRROR:+ / apt=$APT_MIRROR}${NPM_MIRROR:+ / npm=$NPM_MIRROR}"
else
    echo "[INFO] $DOCKERFILE 已存在且符合要求,直接使用(如需重新生成请先删除)"
fi

# ---------- 构建镜像 ----------
echo "[INFO] 开始构建 $IMAGE_NAME:$IMAGE_TAG ..."
echo "       国际版(qodercli):$([ "$WITH_QODER_CLI" -eq 1 ] && echo '含' || echo '不含')"
echo "       DeepSeek(dsh):$([ "$WITH_DEEPSEEK_CLI" -eq 1 ] && echo '含' || echo '不含')"
echo "       Codex(codex):$([ "$WITH_CODEX_CLI" -eq 1 ] && echo '含' || echo '不含')"
echo "       Semgrep(semgrep):$([ "$WITH_SEMGREP" -eq 1 ] && echo '含' || echo '不含')"
echo "       镜像源:${REGISTRY:-默认(docker.io)}${APT_MIRROR:+ / apt=$APT_MIRROR}${NPM_MIRROR:+ / npm=$NPM_MIRROR}"

# DOCKER_BUILDKIT=1 兼容旧版 Docker(23+ 默认已启用,该变量无害)
DOCKER_BUILDKIT=1 docker build -f "$DOCKERFILE" -t "$IMAGE_NAME:$IMAGE_TAG" .
echo "[OK] 镜像构建完成"

# ---------- 验证镜像内工具 ----------
echo "[INFO] 验证镜像内必要工具 ..."
MISSING=0
for cmd in git rg python3 awk find curl; do
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "command -v $cmd" >/dev/null 2>&1; then
        echo "  [OK]   $cmd"
    else
        echo "  [FAIL] $cmd 缺失"
        MISSING=1
    fi
done

if [ "$WITH_SEMGREP" -eq 1 ]; then
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "command -v semgrep" >/dev/null 2>&1; then
        echo "  [OK]   semgrep"
    else
        echo "  [FAIL] semgrep 缺失"
        MISSING=1
    fi
fi

# Node 类 CLI 共享 Node.js 运行时,任一启用就验证 node/npm
if [ "$NEED_NODE" -eq 1 ]; then
    echo "[INFO] 验证 Node.js 运行时(qodercli / dsh / codex 共用)..."
    for cmd in node npm; do
        if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "command -v $cmd" >/dev/null 2>&1; then
            echo "  [OK]   $cmd"
        else
            echo "  [FAIL] $cmd 缺失"
            MISSING=1
        fi
    done
    # 验证 Node 版本 >= 22(qodercli/dsh/codex 统一安装版本)
    NODE_VER=$(docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "node --version" 2>/dev/null || echo "v0.0.0")
    NODE_MAJOR=$(echo "$NODE_VER" | sed -E 's/v([0-9]+)\..*/\1/')
    if [ "$NODE_MAJOR" -ge 22 ] 2>/dev/null; then
        echo "  [OK]   Node 版本 $NODE_VER(>= 22,满足 qodercli/dsh/codex 要求)"
    else
        echo "  [FAIL] Node 版本 $NODE_VER 过低(qodercli >= 20 / dsh >= 20 / codex >= 16,统一装 Node 22.x)"
        MISSING=1
    fi
fi

if [ "$WITH_QODER_CLI" -eq 1 ]; then
    echo "[INFO] 验证 Qoder CLI 国际版依赖 ..."
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "command -v qodercli" >/dev/null 2>&1; then
        echo "  [OK]   qodercli"
    else
        echo "  [FAIL] qodercli 缺失"
        MISSING=1
    fi
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "qodercli --version" >/dev/null 2>&1; then
        echo "  [OK]   qodercli --version 可执行"
    else
        echo "  [WARN] qodercli --version 执行失败(可能是首次需登录,不影响镜像可用性)"
    fi
fi

if [ "$WITH_DEEPSEEK_CLI" -eq 1 ]; then
    echo "[INFO] 验证 DeepSeek Harness CLI 依赖 ..."
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "command -v dsh" >/dev/null 2>&1; then
        echo "  [OK]   dsh"
    else
        echo "  [FAIL] dsh 缺失"
        MISSING=1
    fi
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "dsh --version" >/dev/null 2>&1; then
        echo "  [OK]   dsh --version 可执行"
    else
        echo "  [WARN] dsh --version 执行失败(可能是首次需初始化 profile,不影响镜像可用性)"
    fi
fi

if [ "$WITH_CODEX_CLI" -eq 1 ]; then
    echo "[INFO] 验证 Codex CLI 依赖 ..."
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "command -v codex" >/dev/null 2>&1; then
        echo "  [OK]   codex"
    else
        echo "  [FAIL] codex 缺失"
        MISSING=1
    fi
    if docker run --rm "$IMAGE_NAME:$IMAGE_TAG" bash -lc "codex --version" >/dev/null 2>&1; then
        echo "  [OK]   codex --version 可执行"
    else
        echo "  [WARN] codex --version 执行失败(不影响镜像可用性,运行时按需初始化)"
    fi
fi

if [ "$MISSING" -ne 0 ]; then
    echo "[FAIL] 镜像缺少必要工具,请检查 $DOCKERFILE"
    exit 1
fi

# ---------- 完成 ----------
echo ""
echo "[OK] 全部就绪。在 SecondLook backend/.env 设:"
echo "    SANDBOX_IMAGE=$IMAGE_NAME:$IMAGE_TAG"
if [ "$WITH_QODER_CLI" -eq 1 ] || [ "$WITH_DEEPSEEK_CLI" -eq 1 ] || [ "$WITH_CODEX_CLI" -eq 1 ]; then
    echo ""
    [ "$WITH_QODER_CLI" -eq 1 ] && echo "[OK] Qoder CLI 国际版已预装,支持 task.executor=qoder_cli"
    [ "$WITH_DEEPSEEK_CLI" -eq 1 ] && echo "[OK] DeepSeek Harness CLI 已预装,支持 task.executor=deepseek_cli"
    [ "$WITH_CODEX_CLI" -eq 1 ] && echo "[OK] Codex CLI 已预装,支持 task.executor=codex_cli"
    echo ""
    echo "     凭证配置:用户在「智能体配置」中填入对应凭证即可"
    [ "$WITH_QODER_CLI" -eq 1 ] && echo "       - Qoder PAT:qoder.com/account/integrations 生成"
    [ "$WITH_DEEPSEEK_CLI" -eq 1 ] && echo "       - DeepSeek API Key:platform.deepseek.com 申请(经 DEEPSEEK_API_KEY 注入)"
    [ "$WITH_CODEX_CLI" -eq 1 ] && echo "       - Codex:OpenAI API Key 或自定义 OpenAI 兼容端点(含 base_url + wire_api)"
fi
