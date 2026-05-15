from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response, FileResponse
from fastapi import File, UploadFile
import uuid
from pydantic import BaseModel
from typing import List, Optional
import json
import time
import zipfile
import re
from urllib.parse import unquote

from src.ai_write_x.config.config import Config
from src.ai_write_x.utils.path_manager import PathManager
from src.ai_write_x.tools.wx_publisher import pub2wx
from src.ai_write_x.utils import utils


router = APIRouter(prefix="/api/articles", tags=["articles"])


class ArticleContentUpdate(BaseModel):
    content: str


class PublishRequest(BaseModel):
    article_paths: List[str]
    account_indices: List[int]
    platform: str = "wechat"


class ExportRequest(BaseModel):
    article_paths: List[str]


@router.post("/export-zip")
async def export_articles_zip(request: ExportRequest):
    """批量导出文章为 ZIP，包含 HTML 文件和对应图片"""
    try:
        articles_dir = PathManager.get_article_dir()
        image_dir = PathManager.get_image_dir()

        # 使用 temp 目录存放临时 ZIP 文件
        temp_dir = PathManager.get_temp_dir()
        zip_filename = f"articles_export_{int(time.time())}.zip"
        zip_path = temp_dir / zip_filename

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for article_path_str in request.article_paths:
                file_path = Path(article_path_str)
                if not file_path.exists():
                    continue

                article_name = file_path.stem
                content = file_path.read_text(encoding="utf-8")

                # 收集文章引用的本地图片
                img_pattern = re.compile(r'src=["\'](/images/[^"\']+)["\']')
                referenced_images = set()
                for m in img_pattern.finditer(content):
                    img_path = m.group(1).lstrip("/")
                    referenced_images.add(Path(img_path).name)

                # 写入 HTML 文件
                zip_entry = f"{article_name}/{article_name}.html"
                zf.writestr(zip_entry, content)

                # 写入对应图片
                for img_name in referenced_images:
                    img_file = image_dir / img_name
                    if img_file.exists():
                        zip_entry = f"{article_name}/images/{img_name}"
                        zf.write(img_file, zip_entry)

        return {"status": "success", "download_url": f"/api/articles/download-zip/{zip_filename}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/download-zip/{filename}")
async def download_zip(filename: str):
    """下载已生成的 ZIP 文件"""
    temp_dir = PathManager.get_temp_dir()
    zip_path = temp_dir / filename
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="ZIP 文件不存在或已过期")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename="articles_export.zip",
        headers={"Content-Disposition": "attachment; filename=articles_export.zip"},
    )


@router.get("/")
async def list_articles():
    """获取文章列表"""
    try:
        articles_dir = PathManager.get_article_dir()
        articles = []

        patterns = ["*.html", "*.md", "*.txt"]
        article_files = []

        for pattern in patterns:
            article_files.extend(articles_dir.glob(pattern))

        for file_path in article_files:
            stat = file_path.stat()
            title = file_path.stem.replace("_", "|")
            status = get_publish_status(title)

            articles.append(
                {
                    "name": file_path.stem,
                    "path": str(file_path),
                    "title": title,
                    "format": file_path.suffix[1:].upper(),
                    "size": f"{stat.st_size / 1024:.2f} KB",
                    "create_time": datetime.fromtimestamp(stat.st_ctime).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "status": status,
                }
            )

        articles.sort(key=lambda x: x["create_time"], reverse=True)
        return {"status": "success", "data": articles}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/content")
async def get_article_content(path: str):
    """获取文章内容 - 使用查询参数"""
    file_path = Path(path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文章不存在")

    content = file_path.read_text(encoding="utf-8")
    return Response(content=content, media_type="text/plain; charset=utf-8")


@router.put("/content")
async def update_article_content(path: str, update: ArticleContentUpdate):
    """更新文章内容"""
    file_path = Path(path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文章不存在")

    file_path.write_text(update.content, encoding="utf-8")
    return {"status": "success", "message": "文章已保存"}


@router.get("/preview")
async def preview_article(path: str):
    """安全预览文章 - 使用查询参数"""
    file_path = Path(path)
    if not file_path.exists():
        return HTMLResponse("<p>文章不存在</p>")

    content = file_path.read_text(encoding="utf-8")
    return HTMLResponse(
        content, headers={"Content-Security-Policy": "default-src 'self' 'unsafe-inline'"}
    )


@router.delete("/{article_path:path}")
async def delete_article(article_path: str):
    """删除文章"""
    file_path = Path(article_path)
    if file_path.exists():
        file_path.unlink()
        return {"status": "success", "message": "文章已删除"}
    raise HTTPException(status_code=404, detail="文章不存在")


@router.post("/publish")
async def publish_articles(request: PublishRequest):
    """发布文章到平台"""
    try:
        config = Config.get_instance()
        credentials = config.wechat_credentials

        if not credentials:
            raise HTTPException(status_code=400, detail="未配置微信账号")

        success_count = 0
        fail_count = 0
        error_details = []
        warning_details = []
        format_publish = config.format_publish

        for article_path in request.article_paths:
            file_path = Path(article_path)
            if not file_path.exists():
                fail_count += 1
                error_details.append(f"{article_path}: 文件不存在")
                continue

            content = file_path.read_text(encoding="utf-8")

            ext = file_path.suffix.lower()

            try:
                if ext == ".html":
                    title, digest = utils.extract_html(content)
                elif ext == ".md":
                    title, digest = utils.extract_markdown_content(content)
                elif ext == ".txt":
                    title, digest = utils.extract_text_content(content)
                else:
                    fail_count += 1
                    error_details.append(f"{article_path}: 不支持的文件格式 {ext}")
                    continue
            except Exception as e:
                fail_count += 1
                error_details.append(f"{article_path}: 内容提取失败 - {str(e)}")
                continue

            if title is None:
                fail_count += 1
                error_details.append(f"{article_path}: 标题提取失败，无法发布")
                continue

            for account_index in request.account_indices:
                if account_index >= len(credentials):
                    continue

                cred = credentials[account_index]
                try:
                    article_to_publish = content
                    if ext != ".html" and format_publish:
                        article_to_publish = utils.get_format_article(ext, content)

                    cover_path = utils.get_cover_path(article_path)
                    # 构建代理配置
                    proxy = None
                    if cred.get("proxy_proto") and cred.get("proxy_addr") and cred.get("proxy_port"):
                        proxy = {
                            "proto": cred["proxy_proto"],
                            "addr": cred["proxy_addr"],
                            "port": cred["proxy_port"],
                            "user": cred.get("proxy_user", ""),
                            "pass": cred.get("proxy_pass", ""),
                        }
                    message, _, success = pub2wx(
                        title=title,
                        digest=digest,
                        article=article_to_publish,
                        appid=cred["appid"],
                        appsecret=cred["appsecret"],
                        author=cred.get("author", ""),
                        cover_path=cover_path,
                        draft_only=cred.get("draft_only", False),
                        default_cover_path=cred.get("cover") if not cover_path else None,
                        proxy=proxy,
                    )

                    if success:
                        success_count += 1
                        # 如果message包含权限回收提示,添加到warning_details
                        if message and "草稿箱" in message:
                            warning_details.append(f"{cred.get('author', '未命名')}: {message}")

                        save_publish_record(
                            article_path=article_path,
                            platform="wechat",
                            account_info={
                                "appid": cred["appid"],
                                "author": cred.get("author", ""),
                                "account_type": "wechat_official",
                            },
                            success=True,
                            error=message if "草稿箱" in message else None,
                        )
                    else:
                        fail_count += 1
                        error_details.append(f"{cred.get('author', '未命名')}: {message}")
                        save_publish_record(
                            article_path=article_path,
                            platform="wechat",
                            account_info={
                                "appid": cred["appid"],
                                "author": cred.get("author", ""),
                                "account_type": "wechat_official",
                            },
                            success=False,
                            error=message,
                        )

                except Exception as e:
                    fail_count += 1
                    error_msg = str(e)
                    save_publish_record(
                        article_path=article_path,
                        platform="wechat",
                        account_info={
                            "appid": cred["appid"],
                            "author": cred.get("author", ""),
                            "account_type": "wechat_official",
                        },
                        success=False,
                        error=error_msg,
                    )
                    error_details.append(f"{cred.get('author', '未命名')}: {error_msg}")

        return {
            "status": "success" if success_count > 0 else "error",
            "success_count": success_count,
            "fail_count": fail_count,
            "warning_details": warning_details,
            "error_details": error_details,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def get_publish_status(title: str) -> str:
    """获取文章发布状态"""
    records_file = PathManager.get_article_dir() / "publish_records.json"
    if not records_file.exists():
        return "unpublished"

    try:
        records = json.loads(records_file.read_text(encoding="utf-8"))
        article_records = records.get(title, [])

        if not article_records:
            return "unpublished"

        latest = max(article_records, key=lambda x: x.get("timestamp", ""))
        return "published" if latest.get("success") else "failed"
    except Exception:
        return "unpublished"


def save_publish_record(
    article_path: str, platform: str, account_info: dict, success: bool, error: Optional[str]
):
    """保存发布记录 - 新的通用格式"""
    records_file = PathManager.get_article_dir() / "publish_records.json"

    title = Path(article_path).stem.replace("_", "|")

    records = {}
    if records_file.exists():
        try:
            records = json.loads(records_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    if title not in records:
        records[title] = []

    records[title].append(
        {
            "timestamp": datetime.now().isoformat(),
            "platform": platform,
            "success": success,
            "error": error,
            "account_info": account_info,
        }
    )

    records_file.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


@router.get("/platforms")
async def get_supported_platforms():
    """获取支持的发布平台列表"""
    config = Config.get_instance()

    platforms = []

    # 微信公众号
    wechat_credentials = config.wechat_credentials or []
    if wechat_credentials:
        platforms.append(
            {
                "id": "wechat",
                "name": "微信公众号",
                "icon": "wechat",
                "accounts": [
                    {
                        "index": idx,
                        "author": cred.get("author", "未命名"),
                        "appid": cred["appid"],
                        "full_info": f"{cred.get('author', '未命名')} ({cred['appid']})",
                    }
                    for idx, cred in enumerate(wechat_credentials)
                ],
            }
        )

    # 未来可扩展其他平台
    # if config.other_platform_credentials:
    #     platforms.append({...})

    return {"status": "success", "data": platforms}


@router.get("/publish-history/{article_path:path}")
async def get_publish_history(article_path: str):
    """获取文章发布历史"""
    records_file = PathManager.get_article_dir() / "publish_records.json"

    title = Path(article_path).stem.replace("_", "|")

    if not records_file.exists():
        return {"status": "success", "data": {"article_path": article_path, "records": []}}

    try:
        records = json.loads(records_file.read_text(encoding="utf-8"))
        article_records = records.get(title, [])

        sorted_records = sorted(article_records, key=lambda x: x.get("timestamp", ""), reverse=True)

        return {
            "status": "success",
            "data": {"article_path": article_path, "records": sorted_records},
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ArticleDesign(BaseModel):
    article: str
    html: str
    css: str
    cover: Optional[str] = ""


@router.post("/design")
async def save_article_design(design: ArticleDesign):
    """保存文章设计(包括封面)，封面自动裁剪为 1080x810"""
    try:
        article_path = Path(design.article)
        design_path = article_path.with_suffix(".design.json")

        cover = design.cover
        if cover:
            cover = _crop_cover_to_1080x810(cover)

        design_data = {"html": design.html, "css": design.css, "cover": cover}

        with open(design_path, "w", encoding="utf-8") as f:
            json.dump(design_data, f, ensure_ascii=False, indent=2)

        return {"success": True, "message": "设计已保存"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _crop_cover_to_1080x810(image_src: str) -> str:
    """将封面图裁剪/缩放为 1080x810"""
    try:
        from PIL import Image

        src_path = image_src
        if image_src.startswith("/images/"):
            from src.ai_write_x.utils.path_manager import PathManager
            src_path = str(PathManager.get_image_dir() / image_src.replace("/images/", ""))
        elif not os.path.isabs(image_src):
            src_path = os.path.abspath(image_src)

        if not os.path.exists(src_path):
            return image_src  # 文件不存在，返回原路径

        img = Image.open(src_path)
        src_w, src_h = img.size
        target_w, target_h = 1080, 810
        target_ratio = target_w / target_h
        src_ratio = src_w / src_h

        if src_ratio > target_ratio:
            new_w = int(src_h * target_ratio)
            new_h = src_h
            left = (src_w - new_w) // 2
            top = 0
        else:
            new_w = src_w
            new_h = int(src_w / target_ratio)
            left = 0
            top = (src_h - new_h) // 2

        cropped = img.crop((left, top, left + new_w, top + new_h))
        resized = cropped.resize((target_w, target_h), Image.LANCZOS)

        cover_name = Path(src_path).stem + "_cover.jpg"
        cover_dir = str(Path(src_path).parent)
        cover_path = os.path.join(cover_dir, cover_name)
        resized = resized.convert("RGB")
        resized.save(cover_path, "JPEG", quality=90)

        if image_src.startswith("/images/"):
            return f"/images/{cover_name}"
        return cover_path
    except Exception:
        return image_src  # 裁剪失败时返回原图


@router.get("/design")
async def load_article_design(article: str):
    """加载文章设计(包括封面)"""
    try:
        article_path = Path(article)
        design_path = article_path.with_suffix(".design.json")

        if not design_path.exists():
            return {"html": "", "css": "", "cover": ""}

        with open(design_path, "r", encoding="utf-8") as f:
            design_data = json.load(f)

        return {
            "html": design_data.get("html", ""),
            "css": design_data.get("css", ""),
            "cover": design_data.get("cover", ""),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload-image")
async def upload_image(image: UploadFile = File(...)):
    """上传图片并返回路径"""
    try:
        # 获取图片保存目录
        image_dir = PathManager.get_image_dir()

        # 生成唯一文件名
        file_ext = Path(image.filename).suffix or ".jpg"
        unique_filename = f"{uuid.uuid4().hex}{file_ext}"
        file_path = image_dir / unique_filename

        # 保存图片
        with open(file_path, "wb") as f:
            content = await image.read()
            f.write(content)

        # 返回相对路径(用于 HTML src 属性)
        relative_path = f"/images/{unique_filename}"

        return {"status": "success", "path": relative_path, "filename": unique_filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/images")
async def get_images():
    """获取已上传的图片列表"""
    try:
        image_dir = PathManager.get_image_dir()
        images = []

        # 支持的图片格式
        image_extensions = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

        for file_path in image_dir.iterdir():
            if file_path.is_file() and file_path.suffix.lower() in image_extensions:
                images.append({"filename": file_path.name, "path": f"/images/{file_path.name}"})

        return images
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
