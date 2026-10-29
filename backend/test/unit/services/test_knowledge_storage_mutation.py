"""取消不能把仍在执行的对象写入遗留到清理之后。"""

import asyncio

import pytest

from yuxi.services.knowledge_parse_service import complete_storage_mutation

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("mutation_fails", [False, True])
async def test_cancelled_storage_mutation_waits_for_external_completion(mutation_fails):
    """重复取消仍等待已启动写入收敛，随后把取消交回调用者。"""
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    objects = {}

    async def write_object():
        """用确定性闸门模拟已经启动且不能撤销的对象写入。"""
        started.set()
        await release.wait()
        try:
            if mutation_fails:
                raise RuntimeError("object storage unavailable")
            objects["document.md"] = b"Original paper"
            return "uploaded"
        finally:
            finished.set()

    caller = asyncio.create_task(complete_storage_mutation(write_object()))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done()
        assert not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=1)
        assert finished.is_set()
        assert objects == ({} if mutation_fails else {"document.md": b"Original paper"})
    finally:
        release.set()
        if not caller.done():
            caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)


async def test_storage_mutation_without_cancellation_preserves_storage_error():
    """未取消时真实存储失败不能被吞掉或伪装为上传成功。"""
    async def failed_upload():
        """返回确定性的外部失败。"""
        raise RuntimeError("object storage unavailable")

    with pytest.raises(RuntimeError, match="object storage unavailable"):
        await complete_storage_mutation(failed_upload())
