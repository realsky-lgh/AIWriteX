#!/usr/bin/env python
# -*- coding: UTF-8 -*-

import os
import time
import requests
from pathlib import Path

from src.ai_write_x.utils import log


class ImageSearchTool:
    """Unsplash 图片搜索工具"""

    BASE_URL = "https://api.unsplash.com"

    def __init__(self, access_key: str):
        self.access_key = access_key

    def search(self, query: str, per_page: int = 3) -> list:
        """搜索图片，返回 [{url, thumb_url, author, description}, ...]"""
        if not self.access_key:
            log.print_log("Unsplash Access Key 未配置，跳过图片搜索", "warning")
            return []

        try:
            url = f"{self.BASE_URL}/search/photos"
            headers = {"Authorization": f"Client-ID {self.access_key}"}
            params = {"query": query, "per_page": per_page, "orientation": "landscape"}

            response = requests.get(url, headers=headers, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data.get("results", []):
                results.append({
                    "url": item["urls"]["regular"],
                    "thumb_url": item["urls"]["thumb"],
                    "author": item["user"]["name"],
                    "description": item.get("description") or item.get("alt_description", ""),
                })

            if results:
                log.print_log(f"Unsplash 搜索 '{query}' 返回 {len(results)} 张图片")
            else:
                log.print_log(f"Unsplash 搜索 '{query}' 无结果")

            return results

        except requests.exceptions.RequestException as e:
            log.print_log(f"Unsplash 图片搜索失败: {e}", "error")
            return []
        except Exception as e:
            log.print_log(f"Unsplash 图片搜索异常: {e}", "error")
            return []

    def download_image(self, url: str, save_dir: str) -> str | None:
        """下载图片到本地，返回本地文件路径"""
        try:
            response = requests.get(url, stream=True, allow_redirects=True, timeout=15)
            response.raise_for_status()

            if not os.path.exists(save_dir):
                os.makedirs(save_dir)

            timestamp = int(time.time() * 1000)
            filename = f"unsplash_{timestamp}.jpg"
            filepath = os.path.join(save_dir, filename)

            with open(filepath, "wb") as f:
                for chunk in response.iter_content(8192):
                    f.write(chunk)

            return filepath

        except Exception as e:
            log.print_log(f"下载图片失败: {url} - {e}", "error")
            return None
