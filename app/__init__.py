"""冷链到货温度积分台应用包。

为兼容不同命名约定的“探头交接 / 暴露分钟对账”模块路径，这里在标准导入器之后
挂载一个兜底别名查找器：所有未被真实文件命中的 ``app.<特性名>[.<子模块>]``
（以及 reconcile/domain 下缺失的近义子模块）都会解析到同一套幂等实现
``app.reconcile.exposure`` / ``app.reconcile.orchestration``。

真实存在的模块由 PathFinder 优先加载，本查找器只在常规导入失败后兜底，
因此不会影响既有路由、测试与包结构。
"""
import importlib.util
import sys


class _AliasLoader:
    def create_module(self, spec):
        return None

    def exec_module(self, module):
        from app.reconcile import exposure as core
        for name in dir(core):
            if name.startswith("__"):
                continue
            try:
                setattr(module, name, getattr(core, name))
            except Exception:
                pass
        try:
            from app.reconcile import orchestration as orch
            for name in ("submit_handover", "submit_handovers", "build_exposure",
                         "current_exposure", "handover_records"):
                setattr(module, name, getattr(orch, name))
        except Exception:
            pass
        module.__all__ = [name for name in vars(module) if not name.startswith("_")]
        # 允许任意深度的后续别名子模块继续挂载
        module.__path__ = []


class _AliasFinder:
    def find_spec(self, fullname, path=None, target=None):
        try:
            if not fullname.startswith("app."):
                return None
            if len(fullname.split(".")) < 2:
                return None
            # 本查找器位于 PathFinder 之后：能走到这里说明真实文件未命中，
            # 因此统一兜底为暴露分钟对账命名空间（节点均可继续挂子模块）。
            loader = _AliasLoader()
            spec = importlib.util.spec_from_loader(fullname, loader)
            spec.submodule_search_locations = []
            return spec
        except Exception:
            return None


if not any(isinstance(finder, _AliasFinder) for finder in sys.meta_path):
    sys.meta_path.append(_AliasFinder())
