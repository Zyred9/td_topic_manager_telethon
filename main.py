"""应用入口。

启动顺序:加载配置 → 建目录 → 初始化 DB(建库建表)→ 建默认超管 →
启动 ClientManager 全起小号(M2 接入)→ 挂载 FastAPI 路由与静态资源。

运行:python main.py
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.deps import BizError, result
from config.settings import get_settings
from infra.db import init_schema, run_db
from services import auth_service

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()

    logger.info("初始化数据库...")
    await run_db(init_schema)

    logger.info("检查默认超级管理员...")
    await auth_service.ensure_init_admin()

    logger.info("初始化人设配置标签库(性格/兴趣/emoji)...")
    from services import persona_service
    await persona_service.init_default_configs()

    # 重启不自动恢复任务:定时任务与话题全置停
    from services import schedule_service, keyword_service, topic_scheduler
    await schedule_service.stop_all_on_startup()
    await topic_scheduler.stop_all_on_startup()

    # 注册消息 listener(必须在全起之前,确保全起的号能被监听)
    keyword_service.register()
    topic_scheduler.register()
    from services import dm_service
    dm_service.register()

    # 先完成轻量 DB 预处理，再进入 web/后台任务并发阶段，避免启动批量状态写覆盖新请求。
    from core.client_manager import client_manager
    from services import account_watch_service

    await client_manager.prepare_startup()
    # 历史日志补判死也在并发边界前完成，确保判死后的号不会进入后台全起候选。
    await account_watch_service.scan_dead_on_startup()

    # 逐号全起放后台:串行连接每号最多 15s,几十上百个号可能耗时数分钟；web 不等待。

    async def _background_startup() -> None:
        try:
            logger.info("后台全起已登录小号(不阻塞 web)...")
            await client_manager.startup(
                only_phone="8801736120330",
                only_username="linxi9687",
            )
            # 全起完再启动话题自驱(否则池里还没号,自驱空跑)
            topic_scheduler.start_scheduler()
            logger.info("后台全起完成,话题自驱已启动")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("后台全起异常")

    startup_task = asyncio.create_task(_background_startup())

    # ponytail: 定点初始化观察期间不启动全量巡检，避免巡检补连其他账号。

    logger.info("=" * 50)
    logger.info("服务启动完成(小号后台加载中),根路径 %s,端口 %s",
                settings.server.root_path, settings.server.port)
    logger.info("=" * 50)

    yield

    # 关闭:先停后台全起任务(可能还在连号)
    startup_task.cancel()
    try:
        await startup_task
    except (asyncio.CancelledError, Exception):
        pass

    # 关闭:停调度器 + 巡检 + 定时发送 + 后台任务 + 断开所有 client
    from core.client_manager import client_manager
    from core.lifecycle import task_tracker
    from services import account_watch_service, schedule_service, topic_scheduler
    await topic_scheduler.stop_scheduler()
    await account_watch_service.stop_watch()
    await schedule_service.cancel_all_tasks()
    await task_tracker.cancel_all()
    await client_manager.shutdown()
    logger.info("服务已关闭")


def create_app() -> FastAPI:
    settings = get_settings()
    settings.ensure_dirs()

    app = FastAPI(
        title="TG 小号矩阵管控平台",
        root_path=settings.server.root_path,
        lifespan=lifespan,
    )

    # 业务异常 → 200 + {code, message, data:null}(匹配前端拦截器)
    @app.exception_handler(BizError)
    async def _biz_error_handler(_: Request, exc: BizError) -> JSONResponse:
        return JSONResponse(status_code=200, content=result(code=exc.code, message=exc.message))

    # 请求参数校验失败 → 200 + {code:400}(FastAPI 默认返回 HTTP 422,会绕过下方兜底
    # Exception handler,导致前端拦截器把它当"网络异常"致列表整页空白,故单独接住)
    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        logger.warning("请求参数校验失败: %s", exc.errors())
        return JSONResponse(status_code=200, content=result(code=400, message="请求参数错误"))

    # 兜底异常
    @app.exception_handler(Exception)
    async def _unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("未捕获异常: %s", exc)
        return JSONResponse(status_code=200, content=result(code=500, message=str(exc) or "服务器内部错误"))

    _register_routers(app)

    # 静态资源:头像 + 私聊媒体。前端访问 {root_path}/static/avatar|media/xxx
    app.mount("/static/avatar", StaticFiles(directory=str(settings.avatar_dir)), name="avatar")
    app.mount("/static/media", StaticFiles(directory=str(settings.media_dir)), name="media")

    return app


def _register_routers(app: FastAPI) -> None:
    from api import auth_api, admin_api, account_api, group_op_api

    app.include_router(auth_api.router)
    app.include_router(admin_api.router)
    app.include_router(account_api.router)
    app.include_router(group_op_api.router)

    from api import schedule_api, keyword_api, topic_api, persona_api
    app.include_router(schedule_api.router)
    app.include_router(keyword_api.router)
    app.include_router(topic_api.router)
    app.include_router(persona_api.router)

    from api import dm_api
    app.include_router(dm_api.router)


app = create_app()


def main() -> None:
    _configure_logging()
    settings = get_settings()
    uvicorn.run(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
