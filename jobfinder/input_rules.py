"""Shared input vocabulary; hiring route does not imply employment qualification."""
import re


RECRUITMENT_ALIASES = {
    "社招": "社会招聘", "社会招聘": "社会招聘",
    "校招": "校园招聘", "校园招聘": "校园招聘", "应届生招聘": "校园招聘",
    "实习": "实习", "实习生": "实习", "实习招聘": "实习", "internship": "实习", "intern": "实习",
    "全职": "全职", "fulltime": "全职",
    "兼职": "兼职", "parttime": "兼职",
}


def recruitment_terms(value):
    """OR across explicit labels; retain unknown labels for exact matching."""
    if value is None:
        return []
    values = [value] if isinstance(value, str) else value
    output = []
    for item in values:
        for text in re.split(r"[/、,，;；|]", item):
            text = text.strip()
            if not text:
                continue
            key = re.sub(r"[\s_-]", "", text).casefold()
            label = RECRUITMENT_ALIASES.get(key, text)
            if label not in output:
                output.append(label)
    return output
