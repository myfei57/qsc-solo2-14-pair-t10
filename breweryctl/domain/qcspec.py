"""成品终检规格登记：按工厂与酒种维护放行判定依据。"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..core.ids import new_id, slugify
from ..core.validators import require_text
from ..persistence.store import FileStore
from .models import QcSpec
from .quality import build_metric_spec, default_spec_metrics

QC_SPECS = "qc_specs"

WILDCARD_STYLE = "*"


class QcSpecRegistry:
    """管理终检指标带，支持按酒种精确匹配与工厂级兜底。"""

    def __init__(self, store: FileStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self.specs = store.collection(QC_SPECS)

    def define(
        self,
        brewery_id: str,
        style: str,
        metrics: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """登记或升版一份终检规格。"""

        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        clean_style = WILDCARD_STYLE if style == WILDCARD_STYLE else slugify(
            require_text(style, field="style", max_length=40)
        )
        if not isinstance(metrics, list) or not metrics:
            raise ValidationError("终检指标必须是非空数组", field="metrics")
        built = [build_metric_spec(**item) for item in metrics]
        self._validate_unique(built)
        existing = self._find(clean_brewery, clean_style)
        now = format_moment(self.clock.now())
        if existing is None:
            spec = QcSpec(
                id=new_id("spec"),
                brewery_id=clean_brewery,
                style=clean_style,
                version=1,
                metrics=built,
                created_at=now,
                updated_at=now,
            )
            return self.specs.put(spec.id, spec.to_doc())

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            document["metrics"] = built
            document["version"] = int(document.get("version", 1)) + 1
            document["updated_at"] = now
            return document

        return self.specs.update(str(existing["id"]), mutate)

    def ensure_default(self, brewery_id: str, style: str = WILDCARD_STYLE) -> dict[str, Any]:
        """没有规格时用默认指标带兜底，保证新工厂也能判定。"""

        clean_style = WILDCARD_STYLE if style == WILDCARD_STYLE else slugify(style)
        existing = self._find(brewery_id, clean_style)
        if existing is not None:
            return existing
        return self.define(brewery_id, clean_style, default_spec_metrics())

    def get(self, spec_id: str) -> dict[str, Any]:
        document = self.specs.get(spec_id)
        if document is None:
            raise NotFoundError("终检规格不存在", spec_id=spec_id)
        return document

    def list_specs(self, brewery_id: str | None = None) -> list[dict[str, Any]]:
        items = self.specs.all()
        if brewery_id:
            items = [item for item in items if item.get("brewery_id") == brewery_id]
        return sorted(
            items,
            key=lambda item: (str(item.get("brewery_id")), str(item.get("style"))),
        )

    def require_for_batch(
        self,
        brewery_id: str,
        style: str,
        recipe_version: int | None = None,
    ) -> dict[str, Any]:
        """取批次适用的规格：先酒种精确匹配，再工厂级兜底，最后自动建默认。"""

        clean_style = slugify(require_text(style, field="style", max_length=40))
        exact = self._find(brewery_id, clean_style)
        if exact is not None:
            return exact
        wildcard = self._find(brewery_id, WILDCARD_STYLE)
        if wildcard is not None:
            return wildcard
        return self.ensure_default(brewery_id, WILDCARD_STYLE)

    def _find(self, brewery_id: str, style: str) -> dict[str, Any] | None:
        matches = self.specs.find(
            lambda item: item.get("brewery_id") == brewery_id and item.get("style") == style
        )
        if not matches:
            return None
        matches.sort(key=lambda item: int(item.get("version", 1)), reverse=True)
        return matches[0]

    def _validate_unique(self, metrics: list[dict[str, Any]]) -> None:
        if not metrics:
            raise ValidationError("终检指标不能为空")
        names = [str(item["name"]) for item in metrics]
        if len(set(names)) != len(names):
            raise ConflictError("终检指标名称重复", names=names)
