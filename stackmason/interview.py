"""The conversation that produces a configuration.

Separated from the CLI so it can be driven by a terminal prompt, a config file,
a test, or an agent, without the question logic being duplicated in each. The
transport is swappable; the questions and their validation are not.

Design rules learned from tools that get this wrong:

- **Never ask what can be inferred.** Questions are gated on prior answers, so
  nobody is asked about node groups without having chosen Kubernetes.
- **"I do not know" is a valid answer.** Every sizing question accepts
  ``suggest`` and returns a recommendation with the reasoning attached, because
  the alternative is a user picking at random and blaming the tool later.
- **Validate at the point of asking**, not at generation time. An error twenty
  questions later means starting over.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .stacks.registry import Question, questions_for, resolve_dependencies

# What `suggest` resolves to, and why. The reason travels with the value into
# the generated repo, so six months later the choice is explicable.
SUGGESTIONS: dict[str, tuple[object, str]] = {
    "node_instance_type": (
        "m6i.large",
        "Balanced CPU and memory, current generation, and the default most "
        "Kubernetes workloads should start on. Move to c6i or r6i once you have "
        "measured which resource you actually run out of.",
    ),
    "instance_class": (
        "db.m6g.large",
        "Graviton, cheaper per unit of performance than the Intel equivalent, "
        "and enough headroom to not be the bottleneck while you learn the "
        "workload.",
    ),
    "broker_type": (
        "kafka.m5.large",
        "The smallest broker that does not throttle under sustained production "
        "load. kafka.t3.small is a development size and will mislead you.",
    ),
    "node_type": (
        "cache.t4g.micro",
        "Start small. Cache sizing is driven by working set, which you cannot "
        "know before you run it.",
    ),
}


# How many times a single question may be re-asked before giving up.
MAX_ATTEMPTS = 5


class ValidationError(ValueError):
    """The answer cannot be used. The message is shown to the user verbatim."""


@dataclass
class Answer:
    key: str
    value: object
    stack: str
    reason: str = ""  # populated when the value came from a suggestion


@dataclass
class Interview:
    """Collects answers for a stack selection."""

    stacks: list[str]
    answers: dict[str, object] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.stacks = resolve_dependencies(self.stacks)

    def pending(self) -> list[tuple[str, Question]]:
        """Questions still to ask, skipping any whose condition is unmet."""
        out = []
        for sid, q in questions_for(self.stacks):
            if q.key in self.answers:
                continue
            if q.when and not self.answers.get(q.when):
                continue
            out.append((sid, q))
        return out

    def validate(self, q: Question, raw: object) -> object:
        """Coerce and check one answer. Raises ValidationError with a usable message."""
        if q.kind == "int":
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ValidationError(f"{q.key}: expected a whole number, got {raw!r}") from None
            if q.minimum is not None and value < q.minimum:
                raise ValidationError(f"{q.key}: must be at least {q.minimum}")
            if q.maximum is not None and value > q.maximum:
                raise ValidationError(f"{q.key}: must be at most {q.maximum}")
            return value

        if q.kind == "confirm":
            if isinstance(raw, bool):
                return raw
            s = str(raw).strip().lower()
            if s in {"y", "yes", "true", "1"}:
                return True
            if s in {"n", "no", "false", "0"}:
                return False
            raise ValidationError(f"{q.key}: expected yes or no, got {raw!r}")

        if q.kind == "choice":
            s = str(raw).strip()
            if s not in q.choices:
                raise ValidationError(f"{q.key}: {s!r} is not one of {', '.join(q.choices)}")
            return s

        if q.kind == "multichoice":
            items = raw if isinstance(raw, list) else [x.strip() for x in str(raw).split(",")]
            bad = [i for i in items if i not in q.choices]
            if bad:
                raise ValidationError(f"{q.key}: unknown {bad}; choose from {', '.join(q.choices)}")
            return items

        return str(raw).strip()

    def suggestion_for(self, q: Question) -> tuple[object, str] | None:
        """What `suggest` resolves to for this question, if anything.

        Every question that advertises a recommendation must be answerable with
        `suggest`. Offering advice the parser then rejects is worse than
        offering none: the user types exactly what the prompt implied and is
        told it is invalid.

        Named suggestions win. Otherwise a question with both a recommendation
        and a default resolves to the default, carrying the recommendation text
        as the reason, so the choice stays explicable later.
        """
        if q.key in SUGGESTIONS:
            return SUGGESTIONS[q.key]
        if q.recommend and q.default is not None:
            return (q.default, q.recommend)
        return None

    def answer(self, q: Question, raw: object) -> Answer:
        """Record one answer, resolving `suggest` to a recommendation."""
        if str(raw).strip().lower() in {"suggest", "?"}:
            resolved = self.suggestion_for(q)
            if resolved is None:
                raise ValidationError(f"{q.key}: no suggestion available, please choose a value")
            value, reason = resolved
            self.answers[q.key] = value
            self.reasons[q.key] = reason
            return Answer(q.key, value, "", reason)

        # The ternary ruff suggests here spans ninety characters and obscures
        # that an empty answer means "take the default", which is the most
        # common path through this function.
        if raw in (None, "") and q.default is not None:  # noqa: SIM108
            value = q.default
        else:
            value = self.validate(q, raw)

        self.answers[q.key] = value
        return Answer(q.key, value, "")

    def run(
        self,
        ask: Callable[[Question], object],
        on_error: Callable[[str], None] = lambda m: print(f"  {m}"),
    ) -> dict[str, object]:
        """Drive the interview with any callable that answers a Question.

        Loops rather than iterating a fixed list, because answering one question
        can reveal another.
        """
        guard = 0
        while todo := self.pending():
            guard += 1
            if guard > 500:  # cannot happen; catches a bad `when`
                raise RuntimeError("interview did not converge; check question conditions")
            _, q = todo[0]
            # Bounded. An interactive user gets several tries; a config file or
            # an agent that keeps returning the same invalid value must fail
            # loudly rather than spin, which is what an unbounded retry did.
            for attempt in range(MAX_ATTEMPTS):
                try:
                    self.answer(q, ask(q))
                    break
                except ValidationError as exc:
                    if attempt == MAX_ATTEMPTS - 1:
                        raise ValidationError(
                            f"{exc}  (gave up after {MAX_ATTEMPTS} attempts)"
                        ) from None
                    on_error(str(exc))
        return dict(self.answers)

    def summary(self) -> str:
        lines = [f"stacks: {', '.join(self.stacks)}", ""]
        for k, v in self.answers.items():
            line = f"  {k:26} {v}"
            if k in self.reasons:
                line += "   (suggested)"
            lines.append(line)
        if self.reasons:
            lines.append("")
            lines.append("why the suggested values:")
            for k, why in self.reasons.items():
                lines.append(f"  {k}: {why}")
        return "\n".join(lines)


def render_question(q: Question) -> str:
    """Human-readable prompt, including the recommendation if there is one."""
    parts = [q.prompt]
    if q.kind == "choice":
        parts.append(f"  options: {', '.join(q.choices)}")
    if q.default is not None:
        parts.append(f"  default: {q.default}")
    if q.help:
        parts.append(f"  {q.help}")
    if q.recommend:
        parts.append(f"  suggestion: {q.recommend}")
    return "\n".join(parts)
