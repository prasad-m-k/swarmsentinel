"""Pattern-based content inspection for tool arguments and tool results.

Three detector families: secrets (credential-shaped strings), PII (emails, phone numbers, US SSNs,
Luhn-valid card numbers) and injection markers (phrases that commonly appear in prompt-injection
payloads). These are heuristics with known false positives and misses. A finding is evidence for a
policy decision, not proof; a clean scan does not mean the content is safe (threat model G4).

Findings never carry the matched text, so recorder rows and reports cannot leak what was found.
"""
import re
from dataclasses import dataclass

SECRETS = {
    "private-key": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "gitlab-token": re.compile(r"\bglpat-[\w-]{16,}"),
    "slack-token": re.compile(r"\bxox[abpr]-[\w-]{10,}"),
    "api-key": re.compile(r"\bsk-(?:ant-|proj-)?[\w-]{20,}"),
    "jwt": re.compile(r"\beyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,}"),
    "bearer-token": re.compile(r"(?i)\bbearer\s+[\w.~+/-]{20,}=*"),
    "assigned-secret": re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)\b[\"']?\s*[:=]\s*[\"']?[^\s\"',;]{8,}"),
}

PII = {
    "email": re.compile(r"\b[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
    "phone": re.compile(r"(?<![\w+])(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"),
    "us-ssn": re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
    "payment-card": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
}

INJECTION = {
    "override-instructions": re.compile(
        r"(?i)\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all|your|system)\b[^.\n]{0,20}\b(?:instructions?|prompts?|rules|directions)"),
    "role-reassignment": re.compile(r"(?i)\byou are now\b|\bnew instructions\s*:|\bact as (?:an? )?(?:system|admin|developer)\b"),
    "prompt-exfiltration": re.compile(r"(?i)\b(?:reveal|print|repeat|show)\b[^.\n]{0,30}\b(?:system prompt|hidden instructions|initial prompt)"),
    "concealment": re.compile(r"(?i)\bdo not (?:tell|inform|mention (?:this )?to) the user\b|\bwithout (?:telling|informing) the user\b"),
    "chat-template-token": re.compile(r"<\|(?:im_start|im_end|system|endoftext)\|>|\[/?INST\]|<<SYS>>"),
}

FAMILIES = {"secrets": SECRETS, "pii": PII, "injection": INJECTION}
MAX_SCAN = 200_000


def _luhn(digits):
    total, parity = 0, len(digits) % 2
    for i, d in enumerate(map(int, digits)):
        if i % 2 == parity:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _valid(name, match):
    if name == "payment-card":
        digits = re.sub(r"\D", "", match.group())
        return 13 <= len(digits) <= 19 and _luhn(digits)
    return True


@dataclass(frozen=True)
class Finding:
    family: str
    detector: str
    count: int


def scan(text, families=("secrets", "pii", "injection")):
    """Findings per detector. Only the first MAX_SCAN characters are inspected."""
    text = (text or "")[:MAX_SCAN]
    findings = []
    for family in families:
        for name, pattern in FAMILIES[family].items():
            count = sum(1 for m in pattern.finditer(text) if _valid(name, m))
            if count:
                findings.append(Finding(family, name, count))
    return findings


def redact(text, families):
    """Replace detected spans with [REDACTED:<detector>]. Text beyond MAX_SCAN is withheld, not passed through."""
    text = text or ""
    tail = len(text) > MAX_SCAN
    text = text[:MAX_SCAN]
    for family in families:
        for name, pattern in FAMILIES[family].items():
            text = pattern.sub(lambda m, n=name: f"[REDACTED:{n}]" if _valid(n, m) else m.group(), text)
    return text + ("\n[TRUNCATED: content beyond the inspection limit was withheld]" if tail else "")


def summary(findings):
    return ", ".join(f"{f.detector} x{f.count}" for f in findings)
