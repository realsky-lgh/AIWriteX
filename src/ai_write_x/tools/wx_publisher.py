from dataclasses import dataclass
from enum import Enum
from typing import Optional
from datetime import datetime, timedelta
import requests
from io import BytesIO
from http import HTTPStatus
from urllib.parse import urlparse, unquote
from pathlib import PurePosixPath
from dashscope import ImageSynthesis
import os
import mimetypes
import json
import time
import re
from bs4 import BeautifulSoup  # ✅ 新增：导入 BeautifulSoup

from src.ai_write_x.utils import utils
from src.ai_write_x.config.config import Config
from src.ai_write_x.utils import log
from src.ai_write_x.utils.path_manager import PathManager


# ... (PublishStatus, PublishResult 类保持不变) ...
class PublishStatus(Enum):
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"
    DRAFT = "draft"
    SCHEDULED = "scheduled"


@dataclass
class PublishResult:
    publishId: str
    status: PublishStatus
    publishedAt: datetime
    platform: str
    url: Optional[str] = None


class WeixinPublisher:
    BASE_URL = "https://api.weixin.qq.com/cgi-bin"

    # ... (__init__, is_verified, _ensure_access_token 等方法保持不变) ...
    def __init__(self, app_id: str, app_secret: str, author: str):
        # 获取配置数据，只能使用确定的配置，微信配置是循环发布的，需要传递
        config = Config.get_instance()

        self.access_token_data = None
        self.app_id = app_id
        self.app_secret = app_secret
        self.author = author
        self.img_api_type = config.img_api_type  # 只有一种模型，统一从配置读取
        self.img_api_key = config.img_api_key
        self.img_api_model = config.img_api_model

    @property
    def is_verified(self):
        if not hasattr(self, "_is_verified"):
            url = f"{self.BASE_URL}/account/getaccountbasicinfo?access_token={self._ensure_access_token()}"  # noqa 501
            response = requests.get(url, timeout=5)

            try:
                response.raise_for_status()
                data = response.json()
                wx_verify = data.get("wx_verify_info", {})
                self._is_verified = bool(wx_verify.get("qualification_verify", False))
            except (requests.RequestException, ValueError, KeyError):
                self._is_verified = False

        return self._is_verified

    def _ensure_access_token(self):
        # 检查现有token是否有效
        if self.access_token_data and self.access_token_data[
            "expires_at"
        ] > datetime.now() + timedelta(
            minutes=1
        ):  # 预留1分钟余量
            return self.access_token_data["access_token"]

        # 获取新token
        url = f"{self.BASE_URL}/token?grant_type=client_credential&appid={self.app_id}&secret={self.app_secret}"  # noqa 501

        try:
            response = requests.get(url)
            response.raise_for_status()
            data = response.json()
            access_token = data.get("access_token")
            expires_in = data.get("expires_in")

            if not access_token:
                log.print_log(f"获取access_token失败: {data}")
                return None

            self.access_token_data = {
                "access_token": access_token,
                "expires_in": expires_in,
                "expires_at": datetime.now() + timedelta(seconds=expires_in),
            }
            return access_token
        except requests.exceptions.RequestException as e:
            log.print_log(f"获取微信access_token失败: {e}")

        return None  # 获取不到就返回None，失败交给后面的流程处理

    def _upload_draft(self, article, title, digest, media_id):
        token = self._ensure_access_token()
        url = f"{self.BASE_URL}/draft/add?access_token={token}"

        articles = [
            {
                "title": title[:64],  # 标题长度不能超过64
                "author": self.author,
                "digest": digest[:120],
                "content": article,
                "thumb_media_id": media_id,
                "need_open_comment": 1,
                "only_fans_can_comment": 0,
            },
        ]
        ret = None, None
        try:
            data = {"articles": articles}

            headers = {"Content-Type": "application/json"}
            json_data = json.dumps(data, ensure_ascii=False).encode("utf-8")
            response = requests.post(url, data=json_data, headers=headers)
            response.raise_for_status()
            data = response.json()

            if "errcode" in data and data.get("errcode") != 0:
                ret = None, f"上传草稿失败: {data.get('errmsg')}"
            elif "media_id" not in data:
                ret = None, "上传草稿失败: 响应中缺少 media_id"
            else:
                ret = {"media_id": data.get("media_id")}, None
        except requests.exceptions.RequestException as e:
            ret = None, f"上传微信草稿失败: {e}"

        return ret

    def _generate_img_by_ali(self, prompt, size="1024*1024"):
        image_dir = PathManager.get_image_dir()
        img_url = None
        try:
            rsp = ImageSynthesis.call(
                api_key=self.img_api_key,
                model=self.img_api_model,
                prompt=prompt,
                negative_prompt="低分辨率、错误、最差质量、低质量、残缺、多余的手指、比例不良",
                n=1,
                size=size,
            )
            if rsp.status_code == HTTPStatus.OK:
                # 实际上只有一张图片，为了节约，不同时生成多张
                for result in rsp.output.results:
                    file_name = PurePosixPath(unquote(urlparse(result.url).path)).parts[-1]
                    # 拼接绝对路径和文件名
                    file_path = os.path.join(image_dir, file_name)
                    with open(file_path, "wb+") as f:
                        f.write(requests.get(result.url).content)
                img_url = rsp.output.results[0].url
            else:
                log.print_log(
                    "sync_call Failed, status_code: %s, code: %s, message: %s"
                    % (rsp.status_code, rsp.code, rsp.message)
                )
        except Exception as e:
            log.print_log(f"_generate_img_by_ali调用失败: {e}")

        return img_url

    def generate_img(self, prompt, size="1024*1024"):
        img_url = None
        if self.img_api_type == "ali":
            img_url = self._generate_img_by_ali(prompt, size)
        elif self.img_api_type == "picsum":
            image_dir = str(PathManager.get_image_dir())
            width_height = size.split("*")
            download_url = f"https://picsum.photos/{width_height[0]}/{width_height[1]}?random=1"
            img_url = utils.download_and_save_image(download_url, image_dir)

        return img_url

    def upload_image(self, image_url):
        from src.ai_write_x.utils.utils import resolve_image_path  # 导入新函数

        if not image_url:
            return "SwCSRjrdGJNaWioRQUHzgF68BHFkSlb_f5xlTquvsOSA6Yy0ZRjFo0aW9eS3JJu_", None, None

        ret = None, None, None
        try:
            # 先解析图片路径
            resolved_path = resolve_image_path(image_url)

            if resolved_path.startswith(("http://", "https://")):
                # 处理网络图片
                image_response = requests.get(resolved_path, stream=True)
                image_response.raise_for_status()
                image_buffer = BytesIO(image_response.content)

                mime_type = image_response.headers.get("Content-Type")
                if not mime_type:
                    mime_type = "image/jpeg"
                file_ext = mimetypes.guess_extension(mime_type)
                file_name = "image" + file_ext if file_ext else "image.jpg"
            else:
                # 处理本地图片
                if not os.path.exists(resolved_path):
                    ret = None, None, f"本地图片未找到: {resolved_path}"
                    return ret

                with open(resolved_path, "rb") as f:
                    image_buffer = BytesIO(f.read())

                mime_type, _ = mimetypes.guess_type(resolved_path)
                if not mime_type:
                    mime_type = "image/jpeg"
                file_name = os.path.basename(resolved_path)

            token = self._ensure_access_token()
            if self.is_verified:
                url = f"{self.BASE_URL}/media/upload?access_token={token}&type=image"
            else:
                url = f"{self.BASE_URL}/material/add_material?access_token={token}&type=image"

            files = {"media": (file_name, image_buffer, mime_type)}
            response = requests.post(url, files=files)
            response.raise_for_status()
            data = response.json()

            if "errcode" in data and data.get("errcode") != 0:
                ret = None, None, f"图片上传失败: {data.get('errmsg')}"
            elif "media_id" not in data:
                ret = None, None, "图片上传失败: 响应中缺少 media_id"
            else:
                ret = data.get("media_id"), data.get("url"), None

        except requests.exceptions.RequestException as e:
            ret = None, None, f"图片上传失败: {e}"

        return ret

    def add_draft(self, article, title, digest, media_id):
        ret = None, None
        try:
            # 上传草稿
            draft, err_msg = self._upload_draft(article, title, digest, media_id)
            if draft is not None:
                ret = (
                    PublishResult(
                        publishId=draft["media_id"],
                        status=PublishStatus.DRAFT,
                        publishedAt=datetime.now(),
                        platform="wechat",
                        url=f"https://mp.weixin.qq.com/s/{draft['media_id']}",
                    ),
                    None,
                )
            else:
                ret = None, err_msg
        except Exception as e:
            ret = None, f"微信添加草稿失败: {e}"

        return ret

    def publish(self, media_id: str):
        """
        发布草稿箱中的图文素材

        :param media_id: 要发布的草稿的media_id
        :return: 包含发布任务ID的字典
        """
        ret = None, None
        url = f"{self.BASE_URL}/freepublish/submit"
        params = {"access_token": self._ensure_access_token()}
        data = {"media_id": media_id}

        try:
            response = requests.post(url, params=params, json=data)
            response.raise_for_status()
            result = response.json()

            if "errcode" in result and result.get("errcode") != 0:
                ret = None, f"草稿发布失败: {result.get('errmsg')}"
            elif "publish_id" not in result:
                ret = None, "草稿发布失败: 响应中缺少 publish_id"
            else:
                ret = (
                    PublishResult(
                        publishId=result.get("publish_id"),
                        status=PublishStatus.PUBLISHED,
                        publishedAt=datetime.now(),
                        platform="wechat",
                        url="",  # 需要通过轮询获取
                    ),
                    None,
                )
        except Exception as e:
            ret = None, f"发布草稿文章失败：{e}"

        return ret

    # 轮询获取文章链接
    def poll_article_url(self, publish_id, max_retries=10, interval=2):
        url = f"{self.BASE_URL}/freepublish/get?access_token={self._ensure_access_token()}"
        params = {"publish_id": publish_id}

        for _ in range(max_retries):
            response = requests.post(url, json=params).json()
            if response.get("article_id"):
                return response.get("article_detail")["item"][0]["article_url"]

            time.sleep(interval)

        return None

    # ---------------------以下接口需要微信认证[个人用户不可用]-------------------------
    # 单独发布只能通过绑定到菜单的形式访问到，无法显示到公众号文章列表
    def create_menu(self, article_url):
        ret = ""
        menu_data = {
            "button": [
                {
                    "type": "view",
                    "name": "最新文章",
                    "url": article_url,
                }
            ]
        }
        menu_url = f"{self.BASE_URL}/menu/create?access_token={self._ensure_access_token()}"
        try:
            result = requests.post(menu_url, json=menu_data).json()
            if "errcode" in result and result.get("errcode") != 0:
                ret = f"创建菜单失败: {result.get('errmsg')}"
        except Exception as e:
            ret = f"创建菜单失败:{e}"

        return ret

    # 上传图文消息素材【订阅号与服务号认证后均可用】
    def media_uploadnews(self, article, title, digest, media_id):
        token = self._ensure_access_token()
        url = f"{self.BASE_URL}/media/uploadnews?access_token={token}"

        articles = [
            {
                "thumb_media_id": media_id,
                "author": self.author,
                "title": title[:64],
                "content": article,
                "digest": digest[:120],
                "show_cover_pic": 1,
                "need_open_comment": 1,
                "only_fans_can_comment": 0,
            }
        ]

        ret = None, None
        try:
            data = {"articles": articles}
            headers = {"Content-Type": "application/json"}
            json_data = json.dumps(data, ensure_ascii=False).encode("utf-8")
            response = requests.post(url, data=json_data, headers=headers)
            response.raise_for_status()
            result = response.json()

            if "errcode" in result and result.get("errcode") != 0:
                ret = None, f"上传图文素材失败: {result.get('errmsg')}"
            elif "media_id" not in result:
                ret = None, "上传图文素材失败: 响应中缺少 media_id"
            else:
                ret = result.get("media_id"), None
        except requests.exceptions.RequestException as e:
            ret = None, f"上传微信图文素材失败: {e}"

        return ret

    # 根据标签进行群发【订阅号与服务号认证后均可用】
    def message_mass_sendall(self, media_id, is_to_all=True, tag_id=0):
        ret = None

        if is_to_all:
            data_filter = {
                "is_to_all": is_to_all,
            }
        else:
            if tag_id == 0:
                return "根据标签进行群发失败：未勾选群发，且tag_id=0无效"

            data_filter = {
                "is_to_all": is_to_all,
                "tag_id": tag_id,
            }
        data = {
            "filter": data_filter,
            "mpnews": {"media_id": media_id},
            "msgtype": "mpnews",
            "send_ignore_reprint": 1,
        }
        url = f"{self.BASE_URL}/message/mass/sendall?access_token={self._ensure_access_token()}"

        try:
            result = requests.post(url, json=data).json()
            if "errcode" in result and result.get("errcode") != 0:
                ret = f"根据标签进行群发失败: {result.get('errmsg')}"
        except Exception as e:
            ret = f"群发消息失败：{e}"

        return ret

    def _replace_div_with_section(self, content):
        """
        强制将所有 <div> 标签转换为 <section>
        微信公众号后台对 section 的兼容性更好，且能避免部分 div 样式丢失问题。
        """
        if not content:
            return ""

        try:
            # 使用 html.parser 解析
            soup = BeautifulSoup(content, "html.parser")

            # 查找所有 div 标签并直接修改其 name 属性
            # 这比正则替换更安全，不会误伤文本内容
            for tag in soup.find_all("div"):
                tag.name = "section"

            # 只要把 tag.name 改了，输出时就会变成 <section>...</section>
            return str(soup)

        except Exception as e:
            log.print_log(f"Div转Section失败(bs4): {e}")
            return content

    def _compress_html(self, content, use_compress=True):
        """
        智能压缩HTML（正则版）：
        只负责“清洗”工作：去除换行符和标签间的幽灵空格，防止微信排版错乱。
        """
        if not use_compress or not content:
            return content

        # 1. 移除注释
        content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)

        # 2. 核心修复：只移除“标签 > 后的【换行符+缩进空格】”
        # 逻辑：编辑器里的换行+缩进是多余的，删掉。
        content = re.sub(r">[\n\r]+\s*", ">", content)

        # 3. 移除标签结尾 < 前面的换行和缩进
        content = re.sub(r"\s+<", "<", content)

        # 4. 移除标签之间的纯空白
        content = re.sub(r">\s+<", "><", content)

        # 5. 清理剩余换行符
        content = content.replace("\n", "").replace("\r", "")

        return content

    def _inject_indent(self, content):
        """
        智能注入首行缩进（BS4 终极版）：
        给正文段落添加 text-indent: 2em。
        升级：
        1. 向上查找5层祖先，彻底排除卡片、提示框、嵌套布局。
        2. 增加 box-shadow (阴影) 检测，这是识别卡片的关键。
        3. 排除短文本和特殊符号开头的段落（如注释、列表）。
        """
        if not content:
            return ""

        try:
            soup = BeautifulSoup(content, "html.parser")

            for p in soup.find_all("p"):
                # --- 文本内容检查 (新功能) ---
                text = p.get_text().strip()

                # 1. 空段落或极短段落跳过 (通常是标题、按钮文字或装饰性文字)
                if not text or len(text) < 30:
                    continue

                # 2. 特殊符号开头跳过 (代码注释、伪列表、引用)
                # 您的截图中 "//" 开头的注释就会在这里被豁免
                if text.startswith(
                    ("/", "●", "-", ">", "•", "*", "1.", "2.", "3.", "4.", "5.", "#")
                ):
                    continue

                should_skip = False

                # --- 🛡️ 深度豁免扫描 (查自己 + 往上查5代) ---
                # 检查列表包含：当前标签 p，以及它的父级...
                check_list = [p] + list(p.parents)[:5]

                for node in check_list:
                    if not hasattr(node, "name"):
                        continue

                    # 1.【结构豁免】列表、引用、表格、代码块、按钮
                    if node.name in [
                        "li",
                        "blockquote",
                        "th",
                        "td",
                        "figcaption",
                        "pre",
                        "code",
                        "dt",
                        "dd",
                        "button",
                        "a",
                    ]:
                        should_skip = True
                        break

                    # 获取样式
                    style = node.get("style", "").lower()

                    # 2.【对齐豁免】
                    if "text-align" in style and ("center" in style or "right" in style):
                        should_skip = True
                        break

                    # 3.【布局豁免】Flex / Grid / Inline-Block
                    if "display" in style and (
                        "flex" in style or "grid" in style or "inline-block" in style
                    ):
                        should_skip = True
                        break

                    # 4.【装饰豁免】有背景色、边框、**阴影(关键)**
                    # 只要祖先里有 box-shadow，说明这是个卡片，坚决不缩进
                    if "background" in style or "border" in style or "box-shadow" in style:
                        should_skip = True
                        break

                if should_skip:
                    continue

                # 5.【自身检查】
                p_style = p.get("style", "").lower()
                if "text-indent" in p_style:
                    continue

                # --- 注入样式 ---
                current_style = p.get("style", "")
                new_style = f"text-indent: 2em; {current_style}".strip()
                p["style"] = new_style

            return str(soup)

        except Exception as e:
            log.print_log(f"HTML样式注入失败(bs4): {e}，将使用原始排版")
            return content


def pub2wx(title, digest, article, appid, appsecret, author, cover_path=None, draft_only=False):
    publisher = WeixinPublisher(appid, appsecret, author)
    config = Config.get_instance()
    draft_only = draft_only or getattr(config, 'draft_only', False) or config.get_draft_only_by_appid(appid)

    # 1. 结构标准化：强制 Div -> Section
    # 这是处理的第一步，确保所有容器都是微信友好的 <section>
    article = publisher._replace_div_with_section(article)

    # 2. 样式注入：首行缩进
    # 在 div 变成 section 之后再注入样式，虽然主要针对 p 标签，但层级结构可能变了，所以放在结构调整后
    article = publisher._inject_indent(article)

    # 3. 再处理正文图片URL替换 (bs4 处理后的 html 结构标准，利于正则提取)
    cropped_image_path = ""
    final_image_path = None  # 最终要上传的图片路径

    if cover_path:
        resolved_cover_path = utils.resolve_image_path(cover_path)
        cropped_image_path = utils.crop_cover_image(resolved_cover_path, (900, 384))

        if cropped_image_path:
            final_image_path = cropped_image_path
        else:
            final_image_path = resolved_cover_path
    else:
        # 自动生成封面
        image_url = publisher.generate_img(
            "主题:" + title.split("|")[-1] + ",内容:" + digest,
            "900*384",
        )

        if image_url is None:
            log.print_log("生成图片出错,使用默认图片")
            default_image = utils.get_res_path(
                os.path.join("UI", "bg.png"), os.path.dirname(__file__) + "/../assets/"
            )
            final_image_path = utils.resolve_image_path(default_image)
        else:
            final_image_path = utils.resolve_image_path(image_url)

    # 封面图片上传
    media_id, _, err_msg = publisher.upload_image(final_image_path)

    # 如果使用了临时裁剪文件，上传后删除
    if cover_path and cropped_image_path and cropped_image_path != cover_path:
        try:
            os.remove(cropped_image_path)
        except Exception:
            pass

    if media_id is None:
        return f"封面{err_msg}，无法发布文章", article, False

    # 这里需要将文章中的图片url替换为上传到微信返回的图片url
    try:
        image_urls = utils.extract_image_urls(article)
        for image_url in image_urls:
            # 先解析图片路径
            resolved_path = utils.resolve_image_path(image_url)

            # 判断解析后的路径类型
            if utils.is_local_path(resolved_path):
                # 本地路径处理
                if os.path.exists(resolved_path):
                    _, url, err_msg = publisher.upload_image(resolved_path)
                    if url:
                        article = article.replace(image_url, url)
                    else:
                        log.print_log(f"本地图片上传失败: {image_url}, 错误: {err_msg}")
                else:
                    log.print_log(f"本地图片文件不存在: {resolved_path}")
            else:
                # 网络URL处理
                local_filename = utils.download_and_save_image(
                    resolved_path,
                    str(PathManager.get_image_dir()),
                )
                if local_filename:
                    _, url, err_msg = publisher.upload_image(local_filename)
                    if url:
                        article = article.replace(image_url, url)
                    else:
                        log.print_log(f"网络图片上传失败: {image_url}, 错误: {err_msg}")
                else:
                    log.print_log(f"下载图片失败:{image_url}")
    except Exception as e:
        log.print_log(f"上传配图出错,影响阅读,可继续发布文章:{e}")

    # 4. 在上传给微信前，把所有换行符、缩进空格统统干掉，解决“幽灵空隙”
    article = publisher._compress_html(article)

    # 账号是否认证
    if not publisher.is_verified:
        add_draft_result, err_msg = publisher.add_draft(article, title, digest, media_id)
        if add_draft_result is None:
            return f"{err_msg}，无法发布文章", article, False

        if draft_only:
            return "已存入草稿箱（未发布）", article, True

        publish_result, err_msg = publisher.publish(add_draft_result.publishId)
        if publish_result is None:
            if "api unauthorized" in err_msg:
                return (
                    "自动发布失败，【自2025年7月15日起，个人主体账号、未认证企业账号及不支持认证的账号的发布权限被回收，需到公众号管理后台->草稿箱->发表】",
                    article,
                    True,
                )
            else:
                return f"{err_msg}，无法继续发布文章", article, False
    else:
        if draft_only:
            add_draft_result, err_msg = publisher.add_draft(article, title, digest, media_id)
            if add_draft_result is None:
                return f"{err_msg}，无法存入草稿箱", article, False
            return "已存入草稿箱（未发布）", article, True

        # 显示到列表
        media_id, ret = publisher.media_uploadnews(article, title, digest, media_id)
        if media_id is None:
            if "api unauthorized" in ret:
                return (
                    "账号虽认证（非企业账号），但无发布权限，发布失败，无法自动发布文章",
                    article,
                    False,
                )
            else:
                return f"{ret}，无法显示到公众号文章列表（公众号未认证）", article, False

        """
        article_url = publisher.poll_article_url(publish_result.publishId)
        if article_url is not None:
            # 该接口需要认证,将文章添加到菜单中去，用户可以通过菜单“最新文章”获取到
            ret = publisher.create_menu(article_url)
            if not ret:
                log.print_log(f"{ret}（公众号未认证，发布已成功）")
        else:
            log.print_log("无法获取到文章URL，无法创建菜单（可忽略，发布已成功）")
        """

        # 是否设置为群发
        """
        微信官方说明：https://developers.weixin.qq.com/doc/service/guide/product/message/Batch_Sends.html

        关于群发时设置 is_to_all 为 true 使其进入服务号在微信客户端的历史消息列表的说明：
        设置 is_to_all 为 true 且成功群发，会使得此次群发进入历史消息列表。
        为防止异常，认证服务号在一天内，只能设置 is_to_all 为 true 且成功群发一次，或者在公众平台官网群发一次。以避免一天内有2条群发进入历史消息列表。
        类似地，服务号在一个月内，设置 is_to_all 为 true 且成功群发的次数，加上公众平台官网群发的次数，最多只能是4次。
        服务号设置 is_to_all 为 false 时是可以多次群发的，但每个用户一个月内只会收到最多4条，且这些群发不会进入历史消息列表。
        """
        if config.get_call_sendall_by_appid(appid):
            ret = publisher.message_mass_sendall(
                media_id,
                config.get_sendall_by_appid(appid),
                config.get_tagid_by_appid(appid),
            )
            if ret is not None:
                if "api unauthorized" in ret:
                    return (
                        "没有群发权限，无法显示到公众号文章列表（发布已成功）",
                        article,
                        True,
                    )
                else:
                    return (
                        f"{ret}，无法显示到公众号文章列表（发布已成功）",
                        article,
                        True,
                    )

    return "成功发布文章到微信公众号", article, True
