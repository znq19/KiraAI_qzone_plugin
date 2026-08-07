import random
from collections.abc import Callable


def draw_target(minimum: int, maximum: int, randint: Callable[[int, int], int] = random.randint) -> int:
    minimum = max(0, int(minimum))
    maximum = max(minimum, int(maximum))
    return randint(minimum, maximum)


def build_instruction(target: int, maximum: int) -> str:
    target = max(0, int(target))
    maximum = max(target, int(maximum))
    if target == 0:
        return (
            "本次随机配图目标为0：由你根据内容自主决定是否配图，可选0至"
            f"{maximum}张。"
        )
    return (
        f"本次随机配图目标为{target}张：优先从[近期图片]清单选择恰好{target}张，"
        "并用image_indices传入 qzone_publish；只有可用候选不足时才允许少图或纯文字发布。"
    )


def dedupe_sources(sources: list[str]) -> list[str]:
    return list(dict.fromkeys(source for source in sources if source))


def candidate_label(description: str | None) -> str:
    return description or "图片内容暂未识别（仍可作为配图选择）"


def resolve_described_sources(
    described_sources: list[str],
    chosen_indices: list[int],
    target: int,
    maximum: int,
) -> list[str]:
    selected = dedupe_sources(
        described_sources[index - 1]
        for index in chosen_indices
        if 1 <= index <= len(described_sources)
    )
    if target > 0:
        for source in described_sources:
            if len(selected) >= target:
                break
            if source not in selected:
                selected.append(source)
        return selected[:target]
    return selected[:maximum]
