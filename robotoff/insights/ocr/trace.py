import re
from typing import List, Optional, Union

from robotoff import settings
from robotoff.insights import InsightType
from robotoff.insights.dataclass import RawInsight
from robotoff.insights.ocr.dataclass import OCRField, OCRRegex, OCRResult, get_text
from robotoff.insights.ocr.utils import generate_keyword_processor
from robotoff.utils import text_file_iter
from robotoff.utils.cache import CachedStore


def generate_trace_keyword_processor(labels: Optional[List[str]] = None):
    if labels is None:
        labels = list(text_file_iter(settings.OCR_TRACE_ALLERGEN_DATA_PATH))

    return generate_keyword_processor(labels)


TRACES_REGEX = OCRRegex(
    re.compile(
        r"(?:possibilit[ée] de traces|conditionné dans un atelier qui manipule|peut contenir(?: des traces)?|traces? [ée]ventuelles? d[e']|traces? d[e']|may contain)"
    ),
    field=OCRField.full_text_contiguous,
    lowercase=True,
)

TRACE_KEYWORD_PROCESSOR_STORE = CachedStore(
    fetch_func=generate_trace_keyword_processor, expiration_interval=None
)


def find_traces(content: Union[OCRResult, str]) -> List[RawInsight]:
    insights = []

    text = get_text(content, TRACES_REGEX)

    if not text:
        return []

    processor = TRACE_KEYWORD_PROCESSOR_STORE.get()

    for match in TRACES_REGEX.regex.finditer(text):
        prompt = match.group()
        end_idx = match.end()
        captured = text[end_idx : end_idx + 100]

        for (trace_tag, _), span_start, span_end in processor.extract_keywords(
            captured, span_info=True
        ):
            match_str = captured[span_start:span_end]
            insights.append(
                RawInsight(
                    type=InsightType.trace,
                    value_tag=trace_tag,
                    data={"text": match_str, "prompt": prompt, "notify": False},
                )
            )

    return insights
