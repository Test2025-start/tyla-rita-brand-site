"""共享部署模块 — Git 推送到 GitHub Pages

提供可复用的部署逻辑，供 admin/app.py 和 CLI 工具调用。

增量部署模式：
- 克隆现有 gh-pages 分支到临时目录
- 只替换需要更新的文件（config.js、admin/、assets/）
- 保留其他所有文件（CNAME、README、其他协作者的图片等）
- 使用普通 git push（非 force），保留远程历史
"""
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("deploy")


def deploy_to_github(
    output_dir: str | Path,
    repo_url: str,
    token: str,
    branch: str = "gh-pages",
    subdir: str = "",
    commit_message: str = "Deploy: site update",
) -> tuple[bool, str, str]:
    """
    增量部署到 GitHub Pages。

    无论是否指定 subdir，都使用增量更新模式：
    1. 克隆现有 gh-pages 分支到临时目录
    2. 只替换需要更新的文件
    3. 保留其他所有文件（其他协作者的图片、CNAME 等）
    4. 使用普通 git push（非 force），保留远程历史

    Args:
        output_dir: 生成站点的本地目录
        repo_url: GitHub 仓库 URL
        token: GitHub Personal Access Token
        branch: 推送目标分支，默认 gh-pages
        subdir: 子目录（可选，如 site-dev）
        commit_message: 提交信息

    Returns:
        (成功, 消息, 访问URL)
    """
    output_path = Path(output_dir)

    if not output_path.exists():
        return False, f"输出目录不存在: {output_dir}", ""

    if not repo_url or not token:
        return False, "请先配置 GitHub 仓库 URL 和 Token", ""

    # 拼接带 token 的 URL
    if token and repo_url.startswith("https://"):
        remote_url = repo_url.replace("https://", f"https://{token}@")
    else:
        remote_url = repo_url

    def _run(args: list[str], cwd: str) -> tuple[int, str, str]:
        r = subprocess.run(["git"] + args, cwd=cwd,
                           capture_output=True, text=True)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    # === 增量部署模式：克隆 → 替换 → 推送 ===
    tmpdir = tempfile.mkdtemp(prefix="ghpages_deploy_")
    changed_files = []
    try:
        # 1. 克隆现有 gh-pages 分支
        logger.info("克隆现有 %s 分支...", branch)
        code, _, err = _run(
            ["clone", "--depth", "1", "--branch", branch,
             remote_url, tmpdir], cwd=str(Path(tmpdir).parent))
        if code != 0:
            # 分支不存在，用 git init 新建
            logger.info("%s 分支不存在，创建新分支", branch)
            code, _, err = _run(["init"], tmpdir)
            if code != 0:
                return False, f"git init 失败: {err}", ""
            _run(["remote", "add", "origin", remote_url], tmpdir)
            _run(["checkout", "-b", branch], tmpdir)
        else:
            _run(["remote", "set-url", "origin", remote_url], tmpdir)

        # 2. 确定目标目录
        if subdir:
            target_dir = Path(tmpdir) / subdir
        else:
            target_dir = Path(tmpdir)

        # 3. 增量更新：只替换需要更新的文件/目录
        # 要更新的目录列表
        update_dirs = ["assets", "admin"]
        # 要更新的文件列表
        update_files = ["config.js", "config.live.js", "index.html"]

        # 复制目录
        for dir_name in update_dirs:
            src = output_path / dir_name
            if src.exists():
                dst = target_dir / dir_name
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst, dirs_exist_ok=True)
                changed_files.append(f"{dir_name}/")
                logger.info("已更新目录: %s/", dir_name)

        # 复制文件
        for file_name in update_files:
            src = output_path / file_name
            if src.exists():
                dst = target_dir / file_name
                shutil.copy2(src, dst)
                changed_files.append(file_name)
                logger.info("已更新文件: %s", file_name)

        # 复制其他非目录文件（如 CNAME、favicon 等）
        for item in output_path.iterdir():
            if item.name in (".git",) or item.name in update_dirs or item.name in update_files:
                continue
            dest = target_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
                changed_files.append(f"{item.name}/")
            else:
                shutil.copy2(item, dest)
                changed_files.append(item.name)

        logger.info("增量更新完成，变更 %d 个项目", len(changed_files))

        # 4. 提交并推送（普通 push，不用 force）
        _run(["add", "-A"], tmpdir)
        code, _, err = _run(["commit", "-m", commit_message, "--allow-empty"],
                            tmpdir)
        if code not in (0, 1):
            return False, f"提交失败: {err}", ""

        code, _, err = _run(["push", "origin", branch], tmpdir)
        if code != 0:
            if "rejected" in err.lower() or "non-fast-forward" in err.lower():
                logger.info("检测到远端更新，先拉取合并...")
                _run(["pull", "origin", branch, "--rebase"], tmpdir)
                code, _, err = _run(["push", "origin", branch], tmpdir)
                if code != 0:
                    return False, f"推送失败(合并后): {err}", ""
            elif "Authentication" in err or "403" in err:
                return False, "认证失败，请检查 GitHub Token", ""
            else:
                return False, f"推送失败: {err}", ""

        deploy_url = _get_github_pages_url(repo_url, subdir)
        logger.info("增量部署成功: %s", deploy_url)
        return True, "部署成功（增量更新，已保留其他文件）", deploy_url

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return False, "未知错误", ""


def get_changed_files_summary(output_dir: str | Path, subdir: str = "") -> list[str]:
    """
    获取将要变更的文件列表（用于部署前预览）

    Returns:
        变更的文件/目录名列表
    """
    output_path = Path(output_dir)
    if not output_path.exists():
        return []

    changed = []
    update_dirs = ["assets", "admin"]
    update_files = ["config.js", "config.live.js", "index.html"]

    for dir_name in update_dirs:
        if (output_path / dir_name).exists():
            changed.append(f"{dir_name}/")

    for file_name in update_files:
        if (output_path / file_name).exists():
            changed.append(file_name)

    # 其他文件
    for item in output_path.iterdir():
        if item.name not in (".git",) and item.name not in update_dirs and item.name not in update_files:
            changed.append(item.name + ("/" if item.is_dir() else ""))

    return changed


def _get_github_pages_url(repo_url: str, subdir: str = "") -> str:
    """根据仓库 URL 和子目录推导 GitHub Pages 访问 URL"""
    m = re.match(r'https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?$', repo_url)
    if m:
        user = m.group(1)
        repo = m.group(2)
        url = f"https://{user}.github.io/{repo}/"
        if subdir:
            url += f"{subdir}/"
        return url
    return ""


# 项目根目录 (py-test/)
PROJECT_ROOT = Path(__file__).parent.parent.parent


def get_output_dir(env: str = "dev") -> Path:
    """获取分环境的输出目录（统一在项目根目录下的 output/）"""
    if env == "dev":
        return PROJECT_ROOT / "output" / "site-dev"
    return PROJECT_ROOT / "output" / "site-prod"
