from __future__ import annotations

from tg_forwarder.core.engine import ForwardingEngine


class AuditLogger:
    def __init__(self, engine: ForwardingEngine) -> None:
        self._engine = engine

    async def log_forward(
        self,
        job_name: str,
        src: int,
        msg_id: int,
        dst: int,
        modifications_count: int,
    ) -> None:
        mods = f"修改: {modifications_count}处" if modifications_count else "无修改"
        await self._engine.emit_audit(
            f"[✅ 转发] Job: {job_name} | Source: {src} #{msg_id}\n  {mods}"
        )

    async def log_save(
        self,
        src: int,
        msg_id: int,
        dst: int | str,
        msg_type: str,
        protected: bool = False,
    ) -> None:
        mode = "受保护提取" if protected else "复制"
        await self._engine.emit_audit(
            f"[✅ 保存] 自我转发 | Source: {src} #{msg_id}\n"
            f"  类型: {msg_type} | 目标: {dst} | 方式: {mode}"
        )

    async def log_filter(
        self, job_name: str, src: int, msg_id: int, reason: str
    ) -> None:
        await self._engine.emit_audit(
            f"[❌ 过滤] Job: {job_name} | Source: {src} #{msg_id}\n  原因: {reason}"
        )

    async def log_error(self, context: str, error: Exception | str) -> None:
        await self._engine.emit_audit(
            f"[⚠️ 错误] {context}\n  {error}"
        )
