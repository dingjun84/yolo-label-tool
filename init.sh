#!/usr/bin/env bash
# 初始化当前终端到 yolo-label-tool 的 conda 环境
#
# 用法（必须 source，环境才会作用于当前终端）：
#   source init.sh
#
# 之后可直接运行：
#   python main.py

# 定位脚本所在目录，兼容 bash / zsh（含被 source 的情况）
if [ -n "${BASH_SOURCE:-}" ]; then
    _script="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then
    _script="${(%):-%x}"
else
    _script="$0"
fi
_project_root="$(cd "$(dirname "$_script")" && pwd)"
_env_prefix="$_project_root/.conda/envs/yolo-label-tool"

if [ ! -x "$_env_prefix/bin/python" ]; then
    echo "init.sh: 未找到 conda 环境 $_env_prefix" >&2
    echo "        请先创建：" >&2
    echo "          conda create -y -p \"$_env_prefix\" python=3.11 pip" >&2
    echo "          \"$_env_prefix/bin/pip\" install -r \"$_project_root/requirements.txt\"" >&2
    unset _script _project_root _env_prefix
    return 1 2>/dev/null || exit 1
fi

# 解析 conda：若拿到的是二进制路径（未做过 conda init），需先加载 shell hook
# 才能使用 conda activate；若已是 shell 函数，则直接可用
_conda_cmd="$(command -v conda 2>/dev/null)"
if [ -z "$_conda_cmd" ]; then
    for _c in "${CONDA_EXE:-}" "$HOME/miniconda3/bin/conda" "$HOME/anaconda3/bin/conda" /opt/anaconda3/bin/conda; do
        if [ -n "$_c" ] && [ -x "$_c" ]; then
            _conda_cmd="$_c"
            break
        fi
    done
fi

if [ -z "$_conda_cmd" ]; then
    echo "init.sh: 未找到 conda，请先安装 Miniconda/Anaconda" >&2
    unset _script _project_root _env_prefix _conda_cmd _c
    return 1 2>/dev/null || exit 1
fi

case "$_conda_cmd" in
    /*)
        if [ -n "${ZSH_VERSION:-}" ]; then
            eval "$("$_conda_cmd" shell.zsh hook)"
        else
            eval "$("$_conda_cmd" shell.bash hook)"
        fi
        ;;
esac

# 判断是否被 source（zsh 会把 $0 置为被 source 的文件，故用 ZSH_EVAL_CONTEXT）
_sourced=0
if [ -n "${ZSH_VERSION:-}" ]; then
    case "${ZSH_EVAL_CONTEXT:-}" in
        *:file*) _sourced=1 ;;
    esac
elif [ -n "${BASH_VERSION:-}" ]; then
    [ "${BASH_SOURCE[0]}" != "$0" ] && _sourced=1
fi

if [ "$_sourced" -eq 0 ]; then
    echo "提示：当前是以脚本方式执行的，环境只对这个子进程生效；要作用于当前终端请用：source init.sh"
fi

conda activate "$_env_prefix" || {
    echo "init.sh: conda activate 失败" >&2
    unset _script _project_root _env_prefix _conda_cmd _c _sourced
    return 1 2>/dev/null || exit 1
}

echo "已进入环境: $CONDA_PREFIX"
echo "  python: $(python -V 2>&1)"
echo "  运行程序: python main.py"

unset _script _project_root _env_prefix _conda_cmd _c _sourced