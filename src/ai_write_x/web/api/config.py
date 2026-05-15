#!/usr/bin/env python
# -*- coding: UTF-8 -*-

import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Any
from fastapi import APIRouter, HTTPException, File, UploadFile
from pydantic import BaseModel
import requests
from packaging import version


from src.ai_write_x.version import get_version
from src.ai_write_x.config.config import Config
from src.ai_write_x.utils import log
from src.ai_write_x.utils.path_manager import PathManager
from src.ai_write_x.adapters.platform_adapters import PlatformType


router = APIRouter(prefix="/api/config", tags=["config"])


class ConfigUpdateRequest(BaseModel):
    config_data: Dict[str, Any]


@router.get("/")
async def get_config():
    """获取当前配置"""
    try:
        config = Config.get_instance()
        config_dict = config.config

        config_data = {
            "platforms": config_dict.get("platforms", []),
            "publish_platform": config_dict.get("publish_platform", "wechat"),
            "api": config_dict.get("api", {}),
            "img_api": config_dict.get("img_api", {}),
            "wechat": config_dict.get("wechat", {}),
            "use_template": config_dict.get("use_template", True),
            "template_category": config_dict.get("template_category", ""),
            "template": config_dict.get("template", ""),
            "use_compress": config_dict.get("use_compress", True),
            "aiforge_search_max_results": config_dict.get("aiforge_search_max_results", 10),
            "aiforge_search_min_results": config_dict.get("aiforge_search_min_results", 1),
            "min_article_len": config_dict.get("min_article_len", 1000),
            "max_article_len": config_dict.get("max_article_len", 2000),
            "auto_publish": config_dict.get("auto_publish", False),
            "article_format": config_dict.get("article_format", "html"),
            "format_publish": config_dict.get("format_publish", True),
            "dimensional_creative": config_dict.get("dimensional_creative", {}),
            "image_search": config_dict.get("image_search", {}),
            "aiforge_config": config.aiforge_config,
            "page_design": config_dict.get("page_design"),
        }

        return {"status": "success", "data": config_data}

    except Exception as e:
        log.print_log(f"获取配置失败: {str(e)}", "error")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/")
async def update_config_memory(request: ConfigUpdateRequest):
    """仅更新内存中的配置,不保存到文件"""
    try:
        config = Config.get_instance()
        config_data = request.config_data.get("config_data", request.config_data)

        # 深度合并配置到内存
        def deep_merge(target, source):
            for key, value in source.items():
                if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                    deep_merge(target[key], value)
                else:
                    target[key] = value

        with config._lock:
            if "aiforge_config" in config_data:
                aiforge_config_update = config_data.pop("aiforge_config")
                deep_merge(config.aiforge_config, aiforge_config_update)

            # 处理config.yaml的配置
            deep_merge(config.config, config_data)

        return {"status": "success", "message": "配置已更新(仅内存)"}
    except Exception as e:
        log.print_log(f"更新内存配置失败: {str(e)}", "error")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/")
async def save_config_to_file():
    """保存当前内存配置到文件"""
    try:
        config = Config.get_instance()

        if config.save_config(config.config, config.aiforge_config):
            return {"status": "success", "message": "配置已保存"}
        else:
            raise HTTPException(status_code=500, detail="配置保存失败")
    except Exception as e:
        log.print_log(f"保存配置失败: {str(e)}", "error")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/default")
async def get_default_config():
    """获取默认配置"""
    try:
        config = Config.get_instance()
        return {
            "status": "success",
            "data": {
                **config.default_config,
                "aiforge_config": config.default_aiforge_config,
            },
        }
    except Exception as e:
        log.print_log(f"获取默认配置失败: {str(e)}", "error")
        raise HTTPException(status_code=500, detail=str(e))


def get_ui_config_path():
    """获取 UI 配置文件路径"""
    return PathManager.get_config_dir() / "ui_config.json"


@router.get("/ui-config")
async def get_ui_config():
    """获取 UI 配置"""
    config_file = get_ui_config_path()
    if config_file.exists():
        return json.loads(config_file.read_text(encoding="utf-8"))
    return {"theme": "light", "windowMode": "STANDARD"}


@router.post("/ui-config")
async def save_ui_config(config: dict):
    """保存 UI 配置"""
    config_file = get_ui_config_path()
    config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"success": True}


@router.get("/template-categories")
async def get_template_categories():
    """获取所有模板分类"""
    try:
        from src.ai_write_x.config.config import DEFAULT_TEMPLATE_CATEGORIES

        categories = PathManager.get_all_categories(DEFAULT_TEMPLATE_CATEGORIES)

        return {"status": "success", "data": categories}
    except Exception as e:
        log.print_log(f"获取模板分类失败: {str(e)}", "error")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/templates/{category}")
async def get_templates_by_category(category: str):
    """获取指定分类下的模板列表"""
    try:
        if category == "随机分类":
            return {"status": "success", "data": []}

        templates = PathManager.get_templates_by_category(category)

        return {"status": "success", "data": templates}
    except Exception as e:
        log.print_log(f"获取模板列表失败: {str(e)}", "error")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/platforms")
async def get_platforms():
    """获取所有支持的发布平台"""
    try:
        platforms = [
            {"value": platform_value, "label": PlatformType.get_display_name(platform_value)}
            for platform_value in PlatformType.get_all_platforms()
        ]

        return {"status": "success", "data": platforms}
    except Exception as e:
        log.print_log(f"获取平台列表失败: {str(e)}", "error")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/system-messages")
async def get_system_messages():
    """获取系统消息/帮助信息"""
    config = Config.get_instance()

    # 从配置中读取系统消息
    system_messages = config.config.get("system_messages", [])

    # 如果配置中没有,返回默认消息
    if not system_messages:
        system_messages = [
            {"text": "欢迎使用AIWriteX智能内容创作平台", "type": "info"},
            {"text": "本项目禁止用于商业用途，仅限个人使用", "type": "info"},
            {"text": "技术支持与业务合作，请联系522765228@qq.com", "type": "info"},
            {
                "text": "AIWriteX重新定义AI辅助内容创作的边界，融合搜索+借鉴+AI+创意四重能力，多种超绝玩法，让内容创作充满无限可能",
                "type": "info",
            },
            {"text": "更多AIWriteX功能开发中，敬请期待", "type": "info"},
        ]

    return {"status": "success", "data": system_messages}


@router.get("/page-design")
async def get_page_design_config():
    """获取页面设计配置"""
    config = Config.get_instance()
    page_design = config.get_config().get("page_design")

    # 如果配置不存在,返回None,让前端使用原始HTML
    if not page_design:
        return None

    return page_design


@router.get("/help-manual")
async def get_help_manual():
    """获取使用手册HTML内容"""
    from fastapi.responses import HTMLResponse
    from ..app import templates

    # 渲染模板
    template = templates.get_template("components/help-manual.html")
    html_content = template.render({"request": {}})

    return HTMLResponse(content=html_content)


@router.get("/check-updates")
async def check_for_updates():
    """检查新版本：优先从国内VPS获取，失败则回退到GitHub。便携版跳过。"""
    # 便携版不支持自动更新（未通过 NSIS 安装，无注册表项）
    if not _is_installed_version():
        return {"status": "ok", "has_update": False, "portable": True}

    current_version = get_version()

    # 国内优先：从自有VPS获取更新信息
    vps_url = "http://66.154.119.3/update/version.json"
    github_url = "https://api.github.com/repos/iniwap/AIWriteX/releases/latest"

    for url in (vps_url, github_url):
        try:
            headers = {"User-Agent": "AIWriteX-Client/1.0"}
            if "github" in url:
                headers["Accept"] = "application/vnd.github.v3+json"

            response = requests.get(url, timeout=8, headers=headers)

            if response.status_code == 200:
                data = response.json()

                if "github" in url:
                    latest_version = data["tag_name"].lstrip("Vv")
                    download_url = data["assets"][0]["browser_download_url"] if data.get("assets") else ""
                    release_notes = data.get("body", "")
                else:
                    latest_version = data.get("version", "")
                    download_url = data.get("download_url", "")
                    release_notes = data.get("release_notes", "")

                return {
                    "status": "success",
                    "has_update": version.parse(latest_version) > version.parse(current_version),
                    "current_version": current_version,
                    "latest_version": latest_version,
                    "download_url": download_url,
                    "release_notes": release_notes,
                }
        except Exception:
            continue

    return {"status": "error", "has_update": False, "current_version": current_version}


def _is_installed_version() -> bool:
    """通过 NSIS 写入的注册表项判断是否为安装版"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"Software\Microsoft\Windows\CurrentVersion\Uninstall\AIWriteX"
        )
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


class URLRequest(BaseModel):
    url: str


# ── 自动更新 ──────────────────────────────────────────

_update_download_path = None


@router.post("/download-update")
async def download_update(request: URLRequest):
    """下载新版本安装包到临时目录，返回下载进度"""
    global _update_download_path
    import tempfile
    from pathlib import Path as P

    try:
        url = request.url
        if not url:
            raise HTTPException(status_code=400, detail="缺少下载地址")

        # 下载到临时目录
        tmp_dir = P(tempfile.gettempdir()) / "AIWriteX_Update"
        tmp_dir.mkdir(exist_ok=True)

        filename = url.split("/")[-1] or "AIWriteX_Setup.exe"
        save_path = tmp_dir / filename

        # 流式下载
        resp = requests.get(url, stream=True, timeout=300)
        resp.raise_for_status()

        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)

        _update_download_path = str(save_path)
        return {
            "status": "success",
            "path": str(save_path),
            "size": downloaded,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"下载失败: {str(e)}")


@router.post("/install-update")
async def install_update():
    """运行已下载的安装程序"""
    global _update_download_path
    import subprocess

    if not _update_download_path or not os.path.exists(_update_download_path):
        raise HTTPException(status_code=400, detail="未找到下载的安装包，请先下载")

    try:
        if sys.platform == "win32":
            # DETACHED_PROCESS 确保安装程序在父进程退出后继续运行
            subprocess.Popen(
                f'"{_update_download_path}" /S',
                shell=True,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        return {"status": "success", "message": "安装程序已启动，应用将自动关闭"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"启动安装程序失败: {str(e)}")


@router.post("/open-url")
async def open_external_url(request: URLRequest):
    """打开外部链接"""
    from src.ai_write_x.utils.utils import open_url

    try:
        result = open_url(request.url)
        return {"status": "success", "message": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/upload-cover")
async def upload_cover(file: UploadFile = File(...)):
    """上传自定义封面图片"""
    import shutil

    try:
        # 验证文件类型
        allowed_types = {"image/jpeg", "image/png", "image/webp", "image/gif"}
        if file.content_type and file.content_type not in allowed_types:
            raise HTTPException(status_code=400, detail="仅支持 JPG、PNG、WebP、GIF 格式")

        # 保存到 assets/UI 目录
        import os
        assets_dir = PathManager.get_assets_dir() / "UI"
        assets_dir.mkdir(parents=True, exist_ok=True)

        # 生成唯一文件名
        ext = Path(file.filename).suffix if file.filename else ".png"
        safe_name = f"cover_custom_{int(time.time())}{ext}"
        save_path = assets_dir / safe_name

        with save_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        relative_path = f"UI/{safe_name}"
        return {"status": "success", "path": relative_path, "full_path": str(save_path)}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
