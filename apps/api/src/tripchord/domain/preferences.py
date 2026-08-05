from enum import StrEnum


class PreferenceMode(StrEnum):
    REQUIRED = "required"
    WEIGHTED = "weighted"
    FORBIDDEN = "forbidden"
    INDIFFERENT = "indifferent"

    @property
    def chinese_label(self) -> str:
        return {
            self.REQUIRED: "必须满足",
            self.WEIGHTED: "按重要程度权衡",
            self.FORBIDDEN: "明确禁止",
            self.INDIFFERENT: "不作要求",
        }[self]


__all__ = ["PreferenceMode"]
