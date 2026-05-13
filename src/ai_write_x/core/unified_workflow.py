import os
import time
import re
from pathlib import Path
from typing import Dict, Any, List

from src.ai_write_x.core.base_framework import (
    WorkflowConfig,
    AgentConfig,
    TaskConfig,
    WorkflowType,
    ContentType,
    ContentResult,
)
from src.ai_write_x.adapters.platform_adapters import (
    WeChatAdapter,
    XiaohongshuAdapter,
    DouyinAdapter,
    ToutiaoAdapter,
    BaijiahaoAdapter,
    ZhihuAdapter,
    DoubanAdapter,
)
from src.ai_write_x.core.monitoring import WorkflowMonitor
from src.ai_write_x.config.config import Config
from src.ai_write_x.core.content_generation import ContentGenerationEngine
from src.ai_write_x.utils.path_manager import PathManager
from src.ai_write_x.utils import utils
from src.ai_write_x.adapters.platform_adapters import PlatformType
from src.ai_write_x.utils import log

# 导入维度化创意引擎
from src.ai_write_x.creative.dimensional_engine import DimensionalCreativeEngine


class UnifiedContentWorkflow:
    """统一的内容工作流编排器"""

    def __init__(self):
        self.content_engine = None
        # 移除所有旧创意模块，只保留维度化创意引擎
        self.platform_adapters = {
            PlatformType.WECHAT.value: WeChatAdapter(),
            PlatformType.XIAOHONGSHU.value: XiaohongshuAdapter(),
            PlatformType.DOUYIN.value: DouyinAdapter(),
            PlatformType.TOUTIAO.value: ToutiaoAdapter(),
            PlatformType.BAIJIAHAO.value: BaijiahaoAdapter(),
            PlatformType.ZHIHU.value: ZhihuAdapter(),
            PlatformType.DOUBAN.value: DoubanAdapter(),
        }
        self.monitor = WorkflowMonitor.get_instance()
        # 初始化维度化创意引擎
        config = Config.get_instance()
        dimensional_config = config.dimensional_creative_config
        self.creative_engine = DimensionalCreativeEngine(dimensional_config)

    def get_base_content_config(self, **kwargs) -> WorkflowConfig:
        """动态生成基础内容配置，根据平台和需求定制"""

        config = Config.get_instance()
        # 获取目标平台
        publish_platform = kwargs.get("publish_platform", PlatformType.WECHAT.value)
        writer_des = f"""基于话题'{{topic}}'和搜索工具获取的最新信息，撰写一篇高质量的文章。

工具 aiforge_search_tool 使用参数：
    topic={{topic}}
    urls={{urls}}
    reference_ratio={{reference_ratio}}

执行步骤：
1. 使用 aiforge_search_tool 获取关于'{{topic}}'的最新信息
2. 根据搜索结果的来源类型调整写作策略：
    - 如果是"参考文章"结果：基于提供的参考内容进行创作，根据参考比例调整借鉴程度
    - 如果是"搜索"结果：基于搜索到的信息进行原创写作
    - 优先使用搜索结果中的真实发布时间和数据
    - 如果没有获取到有效结果：使用通用时间表述进行原创写作
3. 确保文章逻辑清晰、内容完整、语言流畅

文章要求：
- 标题：当{{platform}}不为空时为"{{platform}}|{{topic}}"，否则为"{{topic}}"
- 总字数：{config.min_article_len}~{config.max_article_len}字（纯文本字数）
- 格式：标准Markdown格式
- 内容：仅输出最终文章内容，严禁包含思考过程或额外说明"""

        config = Config.get_instance()

        # 基础配置
        agents = [
            AgentConfig(
                role="内容创作专家",
                name="writer",
                goal="撰写高质量文章",
                backstory="你是一位作家",
                tools=["AIForgeSearchTool"],
            ),
        ]

        tasks = [
            TaskConfig(
                name="write_content",
                description=writer_des,
                agent_name="writer",
                expected_output="文章标题 + 文章正文（标准Markdown格式）",
                context=["analyze_topic"],
            ),
        ]

        return WorkflowConfig(
            name=f"{publish_platform}_content_generation",
            description=f"面向{publish_platform}平台的内容生成工作流",
            workflow_type=WorkflowType.SEQUENTIAL,
            content_type=ContentType.ARTICLE,
            agents=agents,
            tasks=tasks,
        )

    def _generate_base_content(self, topic: str, **kwargs) -> ContentResult:
        """生成基础内容"""
        # 动态获取配置
        base_config = self.get_base_content_config(**kwargs)

        # 创建内容生成引擎
        self.content_engine = ContentGenerationEngine(base_config)

        # 准备输入数据
        input_data = {
            "topic": topic,
            "platform": kwargs.get("platform", ""),
            "urls": kwargs.get("urls", []),
            "reference_ratio": kwargs.get("reference_ratio", 0.0),
        }

        return self.content_engine.execute_workflow(input_data)

    def execute(self, topic: str, **kwargs) -> Dict[str, Any]:
        """统一执行流程：输入 -> 内容生成 -> 格式处理 -> 保存 -> 发布"""
        start_time = time.time()
        success = False
        config = Config.get_instance()
        publish_platform = config.publish_platform
        # 构建标题：platform|topic 格式
        platform = kwargs.get("platform", "")

        if platform:
            title = f"{platform}|{topic}"
        else:
            title = topic

        try:
            # 1. 生成基础内容（统一Markdown格式）
            base_content = self._generate_base_content(
                topic, publish_platform=publish_platform, **kwargs
            )
            log.print_log("[PROGRESS:WRITING:END]", "internal")

            # 2. 维度化创意变换
            log.print_log("[PROGRESS:CREATIVE:START]", "internal")
            final_content = self._apply_dimensional_creative_transformation(base_content, **kwargs)
            log.print_log("[PROGRESS:CREATIVE:END]", "internal")

            # 2.5. 搜索配图（配置开关控制）
            image_urls = self._fetch_images(title, **kwargs)
            kwargs["image_urls"] = image_urls

            # 3. 转换处理（template或design）

            transform_content = self._transform_content(final_content, publish_platform, **kwargs)

            # 4. 保存（非AI参与）
            log.print_log("[PROGRESS:SAVE:START]", "internal")
            save_result = self._save_content(transform_content, title)
            if save_result.get("success", False):
                article_path = save_result.get("path")
                kwargs["article_path"] = article_path
                # 为文章保存封面图，确保发布时有封面可用
                self._persist_article_cover(article_path, image_urls)
                log.print_log(f"文章《{title}》保存成功！")
            log.print_log("[PROGRESS:SAVE:END]", "internal")

            # 5. 可选发布（非AI参与，开关控制）
            publish_result = None
            if self._should_publish():
                log.print_log("[PROGRESS:PUBLISH:START]", "internal")
                publish_result = self._publish_content(
                    transform_content, publish_platform, **kwargs
                )
                log.print_log(f"发布完成，总结：{publish_result.get('message')}")

                log.print_log("[PROGRESS:PUBLISH:END]", "internal")

            results = {
                "base_content": base_content,
                "final_content": final_content,
                "formatted_content": transform_content.content,
                "save_result": save_result,
                "publish_result": publish_result,
                "success": True,
            }

            success = True
            return results

        except Exception as e:
            self.monitor.log_error("unified_workflow", str(e), {"topic": topic})
            raise
        finally:
            duration = time.time() - start_time
            self.monitor.track_execution("unified_workflow", duration, success, {"topic": topic})

    def _transform_content(
        self, content: ContentResult, publish_platform: str, **kwargs
    ) -> ContentResult:
        """内容转换：template或design路径的AI处理"""
        config = Config.get_instance()
        adapter = self.platform_adapters.get(publish_platform)

        if not adapter:
            raise ValueError(f"不支持的平台: {publish_platform}")

        # AI驱动的内容转换
        if adapter.supports_html() and config.article_format.upper() == "HTML":
            if config.use_template and adapter.supports_template():
                return self._apply_template_formatting(content, **kwargs)
            else:
                return self._apply_design_formatting(content, publish_platform, **kwargs)
        else:
            return content

    def _apply_template_formatting(self, content: ContentResult, **kwargs) -> ContentResult:
        """Template路径：使用AI填充本地模板"""
        log.print_log("[PROGRESS:TEMPLATE:START]", "internal")

        template_config = self._get_template_workflow_config(**kwargs)
        engine = ContentGenerationEngine(template_config)

        input_data = {
            "content": content.content,
            "title": content.title,
            "parse_result": False,
            "content_format": "html",
            **kwargs,
        }

        ret = engine.execute_workflow(input_data)
        ret = self._strip_ai_artifacts(ret)
        log.print_log("[PROGRESS:TEMPLATE:END]", "internal")

        return ret

    def _apply_design_formatting(
        self, content: ContentResult, publish_platform: str, **kwargs
    ) -> ContentResult:
        """Design路径：使用AI生成HTML设计"""
        log.print_log("[PROGRESS:DESIGN:START]", "internal")

        design_config = self._get_design_workflow_config(publish_platform, **kwargs)
        engine = ContentGenerationEngine(design_config)

        input_data = {
            "content": content.content,
            "title": content.title,
            "platform": publish_platform,
            "parse_result": False,
            "content_format": "html",
            **kwargs,
        }

        ret = engine.execute_workflow(input_data)
        ret = self._strip_ai_artifacts(ret)
        log.print_log("[PROGRESS:DESIGN:END]", "internal")

        return ret

    def _apply_dimensional_creative_transformation(
        self, base_content: ContentResult, **kwargs
    ) -> ContentResult:
        """维度化创意变换"""
        config = Config.get_instance()
        dimensional_config = config.dimensional_creative_config

        # 检查是否启用维度化创意
        if not dimensional_config.get("enabled", False):
            return base_content

        # 重新初始化维度化创意引擎以获取最新配置
        self.creative_engine = DimensionalCreativeEngine(dimensional_config)

        # 应用维度化创意变换
        try:
            transformed_content = self.creative_engine.apply_dimensional_creative(
                base_content.content, base_content.title
            )

            # 创建新的ContentResult对象 - 包含所有必需参数
            result = ContentResult(
                title=base_content.title,
                content=transformed_content,
                summary=base_content.summary,  # 添加缺失的summary参数
                content_format=base_content.content_format,  # 添加缺失的content_format参数
                metadata=base_content.metadata.copy(),
            )

            # 添加变换元数据
            result.metadata.update(
                {
                    "transformation_type": "dimensional_creative",
                    "original_content_id": id(base_content),
                    "creative_engine_config": dimensional_config,
                }
            )

            return result

        except Exception as e:
            log.print_log(f"维度化创意变换失败: {str(e)}", "error")
            return base_content

    def _fetch_images(self, topic: str, **kwargs) -> list:
        """根据文章主题搜索配图，返回图片路径列表"""
        config = Config.get_instance()
        if not config.image_search_enabled:
            return []

        access_key = config.image_search_access_key
        if not access_key:
            log.print_log("未配置 Unsplash Access Key，跳过图片搜索", "warning")
            return []

        count = kwargs.get("image_count") or config.image_search_count
        if count <= 0:
            return []

        try:
            from src.ai_write_x.tools.image_search import ImageSearchTool

            keyword = config.image_search_keyword or topic.split("|")[-1].strip()
            searcher = ImageSearchTool(access_key)
            results = searcher.search(keyword, count)

            image_dir = PathManager.get_image_dir()
            local_paths = []
            for r in results:
                path = searcher.download_image(r["url"], str(image_dir))
                if path:
                    filename = Path(path).name
                    local_paths.append(f"/images/{filename}")

            if local_paths:
                log.print_log(f"配图搜索完成：{len(local_paths)}/{count} 张")
            return local_paths

        except Exception as e:
            log.print_log(f"配图搜索异常: {e}", "error")
            return []

    def _strip_ai_artifacts(self, result: ContentResult) -> ContentResult:
        """清理AI输出中残留的思考过程、适配说明等元描述文字"""
        if not result or not result.content:
            return result

        content = result.content

        # 移除HTML标签之前的AI规划/分析文本
        html_match = re.search(
            r'(<!DOCTYPE|<html|<section|<div|<body|<article|<head|<style)',
            content, re.IGNORECASE
        )
        if html_match:
            content = content[html_match.start():]

        # 移除 "Thought:" / "Thought：" 开头的思考块
        content = re.sub(
            r'Thought[：:]\s*[^\n]*(\n|$)', '', content, flags=re.IGNORECASE
        )

        # 移除 "**适配说明：**" 及其后续所有内容
        content = re.sub(
            r'\*\*适配说明[：:]\*\*[\s\S]*$', '', content
        )

        # 移除 "**修改说明：**" 及其后续所有内容
        content = re.sub(
            r'\*\*修改说明[：:]\*\*[\s\S]*$', '', content
        )

        # 移除 "现在开始编写完整的HTML" 等过渡语（单独成行）
        content = re.sub(
            r'^.*(现在开始编写|开始填充内容|让我开始适配|开始适配).*\n?', '',
            content, flags=re.MULTILINE | re.IGNORECASE
        )

        result.content = content.strip()
        return result

    def _get_template_workflow_config(
        self, publish_platform: str = PlatformType.WECHAT.value, **kwargs
    ) -> WorkflowConfig:
        """生成模板处理工作流配置"""
        # 获取配置以获取字数限制
        config = Config.get_instance()

        if publish_platform == PlatformType.WECHAT.value:
            # 微信平台的详细模板填充要求
            task_description = f"""
# HTML内容适配任务
## 任务目标
使用工具 read_template_tool 读取本地HTML模板，将以下文章内容适配填充到HTML模板中：

**文章内容：**
{{content}}

**文章标题：**
{{title}}

## 执行步骤
1. 首先使用 read_template_tool 读取HTML模板
2. 分析模板的结构、样式和布局特点
3. 获取前置任务生成的文章内容
4. 将新内容按照模板结构进行适配填充
5. 确保最终输出是基于原模板的HTML，保持视觉效果和风格不变

## 具体要求
- 分析HTML模板的结构、样式和布局特点
- 识别所有内容占位区域（标题、副标题、正文段落、引用、列表等）
- 将新文章内容按照原模板的结构和布局规则填充：
    * 保持<section>标签的布局结构和内联样式不变
    * 保持原有的视觉层次、色彩方案和排版风格
    * 保持原有的卡片式布局、圆角和阴影效果
    * 保持SVG动画元素和交互特性

- 内容适配原则：
    * 标题替换标题、段落替换段落、列表替换列表
    * 内容总字数{config.min_article_len}~{config.max_article_len}字，不可过度删减前置任务生成的文章内容
    * 当新内容比原模板内容长或短时，合理调整，不破坏布局
    * 保持原有的强调部分（粗体、斜体、高亮等）应用于新内容的相应部分
    * 保持图片位置
    * 不可使用模板中的任何日期作为新文章的日期

- 严格限制：
    * 不添加新的style标签或外部CSS
    * 不改变原有的色彩方案（限制在三种色系内）
    * 不修改模板的整体视觉效果和布局结构
    * 严禁在文章末尾或任何位置添加"适配说明"、"修改说明"、注释、思考过程或任何形式的元描述文字
    * 最终输出必须是纯净的HTML文章内容，不含任何额外说明"""

            # 注入配图URL
            image_urls = kwargs.get("image_urls", [])
            if image_urls:
                image_list = "\n".join(f"  - {url}" for url in image_urls)
                task_description += f"\n\n## 可用配图（已按主题搜索，请替换模板中原有图片）\n{image_list}"

            backstory = "你是微信公众号模板处理专家，能够将内容适配到HTML模板中。严格按照以下要求：保持<section>标签的布局结构和内联样式不变、保持原有的视觉层次、色彩方案和排版风格、不可使用模板中的任何日期作为新文章的日期"  # noqa 501
        else:
            # 其他平台的简化模板处理
            task_description = "使用工具 read_template_tool 读取本地模板，将内容适配填充到模板中"
            backstory = "你是模板处理专家，能够将内容适配到模板中"

        agents = [
            AgentConfig(
                role="模板调整与内容填充专家",
                name="templater",
                goal="根据文章内容，适当调整给定的HTML模板，去除原有内容，并填充新内容。",
                backstory=backstory,
                tools=["ReadTemplateTool"],
            )
        ]

        tasks = [
            TaskConfig(
                name="template_content",
                description=task_description,
                agent_name="templater",
                expected_output="填充新内容但保持原有视觉风格的文章（HTML格式），严禁包含适配说明、思考过程、Thought或任何额外注释文字",
            )
        ]

        return WorkflowConfig(
            name="template_formatting",
            description="模板格式化工作流",
            workflow_type=WorkflowType.SEQUENTIAL,
            content_type=ContentType.ARTICLE,
            agents=agents,
            tasks=tasks,
        )

    def _get_design_workflow_config(self, publish_platform: str, **kwargs) -> WorkflowConfig:
        """生成设计工作流配置"""

        # 微信平台的完整系统模板
        wechat_system_template = """<|start_header_id|>system<|end_header_id|>
# 严格按照以下要求进行微信公众号排版设计：
## 设计目标：
    - 创建一个美观、现代、易读的"**中文**"的移动端网页，具有以下特点：
    - 纯内联样式：不使用任何外部CSS、JavaScript文件，也不使用<style>标签
    - 移动优先：专为移动设备设计，不考虑PC端适配
    - 模块化结构：所有内容都包裹在<section style="xx">标签中
    - 简洁结构：不包含<header>和<footer>标签
    - 视觉吸引力：创造出视觉上令人印象深刻的设计

## 设计风格指导:
    - 色彩方案：使用大胆、酷炫配色、吸引眼球，反映出活力与吸引力，但不能超过三种色系，长久耐看，间隔合理使用，出现层次感。
    - 读者感受：一眼喜欢，很高级，很震惊，易读易懂
    - 排版：符合中文最佳排版实践，利用不同字号、字重和间距创建清晰的视觉层次，风格如《时代周刊》、《VOGUE》
    - 卡片式布局：使用圆角、阴影和边距创建卡片式UI元素
    - 图片处理：大图展示，配合适当的圆角和阴影效果

## 技术要求:
    - 纯 HTML 结构：只使用 HTML 基本标签和内联样式
    - 这不是一个标准HTML结构，只有div和section包裹，但里面可以用任意HTML标签
    - 内联样式：所有样式和字体都通过style属性直接应用在<section>这个HTML元素上，其他都没有style,包括body
    - 模块化：使用<section>标签包裹不同内容模块
    - 简单交互：用HTML原生属性实现微动效
    - 图片处理：系统已搜索与主题相关的配图（见任务描述中的图片URL列表），请合理插入到文章合适位置，大图展示+圆角阴影，每张图至少用一次
    - SVG：生成炫酷SVG动画，目的是方便理解或给用户小惊喜
    - SVG图标：采用Material Design风格的现代简洁图标，支持容器式和内联式两种展示方式
    - 只基于核心主题内容生成，不包含作者，版权，相关URL等信息

## 其他要求：
    - 先思考排版布局，然后再填充文章内容
    - 输出长度：10屏以内 (移动端)
    - 生成的代码**必须**放在`` 标签中
    - 主体内容必须是**中文**，但可以用部分英语装逼
    - 不能使用position: absolute
<|eot_id|>"""

        # 根据平台定制设计要求
        platform_requirements = {
            PlatformType.WECHAT.value: "微信公众号HTML设计要求：使用内联CSS样式，避免外部样式表；采用适合移动端阅读的字体大小和行距；使用微信官方推荐的色彩搭配；确保在微信客户端中显示效果良好",  # noqa 501
            PlatformType.XIAOHONGSHU.value: "小红书平台设计要求：注重视觉美感，使用年轻化的设计风格；适当使用emoji和装饰元素；保持简洁清新的排版",
            PlatformType.ZHIHU.value: "知乎平台设计要求：专业简洁的学术风格；重视内容的逻辑性和可读性；使用适合长文阅读的排版",
        }

        design_requirement = platform_requirements.get(
            publish_platform, "通用HTML设计要求：简洁美观，注重用户体验"
        )

        agents = [
            AgentConfig(
                role="微信排版专家",
                name="designer",
                goal=f"为{publish_platform}平台创建精美的HTML设计和排版",
                backstory="你是HTML设计专家",
                system_template=(
                    wechat_system_template
                    if publish_platform == PlatformType.WECHAT.value
                    else None
                ),
                prompt_template="<|start_header_id|>user<|end_header_id|>{{ .Prompt }}<|eot_id|>",
                response_template="<|start_header_id|>assistant<|end_header_id|>{{ .Response }}<|eot_id|>",  # noqa 501
            )
        ]

        image_urls = kwargs.get("image_urls", [])
        image_hint = ""
        if image_urls:
            image_list = "\n".join(f"  - {url}" for url in image_urls)
            image_hint = f"\n\n## 可用配图（已按主题搜索，请合理插入）\n{image_list}"

        tasks = [
            TaskConfig(
                name="design_content",
                description=f"为{publish_platform}平台设计HTML排版。{design_requirement}。创建精美的HTML格式，包含适当的标题层次、段落间距、颜色搭配和视觉元素，确保内容在{publish_platform}平台上有最佳的展示效果。{image_hint}",  # noqa 501
                agent_name="designer",
                expected_output=f"针对{publish_platform}平台优化的精美HTML内容，严禁包含适配说明、思考过程、Thought或任何额外注释文字",
            )
        ]

        return WorkflowConfig(
            name=f"{publish_platform}_design",
            description=f"面向{publish_platform}平台的HTML设计工作流",
            workflow_type=WorkflowType.SEQUENTIAL,
            content_type=ContentType.ARTICLE,
            agents=agents,
            tasks=tasks,
        )

    def _save_content(self, content: ContentResult, title: str) -> Dict[str, Any]:
        """保存内容（非AI参与）"""
        config = Config.get_instance()
        # 确定文件格式和路径
        file_extension = utils.get_file_extension(config.article_format)
        save_path = self._get_save_path(title, file_extension)

        # 保存文件
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(content.content)

        return {"success": True, "path": save_path, "title": title, "format": config.article_format}

    def _get_save_path(self, title: str, file_extension: str) -> str:
        """获取保存路径"""

        # 获取文章保存目录
        dir_path = PathManager.get_article_dir()

        # 清理文件名，确保安全
        safe_filename = utils.sanitize_filename(title)

        # 构建完整路径
        save_path = os.path.join(dir_path, f"{safe_filename}.{file_extension}")

        return save_path

    def _publish_content(
        self, content: ContentResult, publish_platform: str, **kwargs
    ) -> Dict[str, Any]:
        """发布内容（非AI参与）"""
        adapter = self.platform_adapters.get(publish_platform)

        if not adapter:
            return {"success": False, "message": f"不支持的平台: {publish_platform}"}

        # 将 cover_path 传递给适配器
        kwargs["cover_path"] = utils.get_cover_path(kwargs.get("article_path"))

        # 使用平台适配器发布
        # 适配器内部会自动保存发布记录
        publish_result = adapter.publish_content(content, **kwargs)

        return {
            "success": publish_result.success,
            "message": publish_result.message,
            "platform": publish_platform,
        }

    def _should_publish(self) -> bool:
        """判断是否应该发布"""
        config = Config.get_instance()

        # 检查配置中的自动发布设置
        if not config.auto_publish:
            return False

        # 检查是否有有效的微信凭据
        valid_credentials = any(
            cred["appid"] and cred["appsecret"] for cred in config.wechat_credentials
        )

        if not valid_credentials:
            # 自动转为非自动发布并提示
            log.print_log("检测到自动发布已开启，但未配置有效的微信公众号凭据", "warning")
            log.print_log("请在配置中填写 appid 和 appsecret 以启用自动发布功能", "warning")
            log.print_log("当前将跳过发布步骤，仅生成内容", "info")
            return False

        return True

    def _persist_article_cover(self, article_path: str, image_urls: list) -> None:
        """为文章保存封面图到 .design.json，确保发布时有封面可用"""
        import json
        from pathlib import Path

        design_path = Path(article_path).with_suffix(".design.json")
        design_data = {}
        if design_path.exists():
            try:
                design_data = json.loads(design_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        # 已有封面则跳过
        if design_data.get("cover"):
            return

        # 优先使用搜索配图的第一张
        if image_urls and len(image_urls) > 0:
            design_data["cover"] = image_urls[0]
            design_path.write_text(json.dumps(design_data, ensure_ascii=False, indent=2), encoding="utf-8")
            log.print_log("已自动设置文章封面（来自搜索配图）")
            return

        # 回退：使用凭证的自定义默认封面
        config = Config.get_instance()
        credentials = config.wechat_credentials
        for cred in credentials:
            cover_path = cred.get("cover")
            if cover_path:
                # 转换为绝对路径：凭证中存储的是相对 assets 目录的路径
                import os
                assets_dir = os.path.normpath(
                    os.path.join(os.path.dirname(__file__), "..", "assets")
                )
                abs_cover = os.path.join(assets_dir, cover_path)
                if os.path.exists(abs_cover):
                    design_data["cover"] = abs_cover
                    design_path.write_text(json.dumps(design_data, ensure_ascii=False, indent=2), encoding="utf-8")
                    log.print_log("已自动设置文章封面（来自公众号默认封面）")
                    return

    def get_performance_report(self) -> Dict[str, Any]:
        """获取性能报告"""
        return {
            "workflow_metrics": self.monitor.get_metrics(),
            "recent_executions": self.monitor.get_recent_logs(limit=20),
            "system_status": "healthy" if self._check_system_health() else "degraded",
        }

    def _check_system_health(self) -> bool:
        """检查系统健康状态"""
        metrics = self.monitor.get_metrics()
        for workflow_name, workflow_metrics in metrics.items():
            if workflow_metrics.get("success_rate", 0) < 0.8:  # 成功率低于80%
                return False
        return True

    def register_platform_adapter(self, name: str, adapter):
        """注册新的平台适配器"""
        self.platform_adapters[name] = adapter


class BatchWorkflow:
    """批量内容生成编排器 - 串行模式，每次迭代使用全新 workflow 实例避免状态污染"""

    def execute_batch(self, topics: List[str], **kwargs) -> Dict[str, Any]:
        results = []
        total = len(topics)
        for i, topic in enumerate(topics):
            topic = topic.strip()
            if not topic:
                continue
            log.print_log(f"[PROGRESS:BATCH:{i+1}/{total}] 开始生成: {topic}", "internal")
            try:
                workflow = UnifiedContentWorkflow()
                result = workflow.execute(topic=topic, **kwargs)
                result["topic"] = topic
                result["index"] = i
                results.append(result)
            except Exception as e:
                log.print_log(f"[PROGRESS:BATCH:{i+1}/{total}] 生成失败: {topic} - {e}", "error")
                results.append({"topic": topic, "index": i, "success": False, "error": str(e)})
        success_count = sum(1 for r in results if r.get("success"))
        return {"batch_results": results, "total": total, "success_count": success_count}
