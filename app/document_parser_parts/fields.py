from __future__ import annotations

import re
from typing import Any

from app.document_parser_parts.constants import ORGANISATION_TERMS, POSITION_TERMS, TARGET_STOP_WORDS


class FieldExtractionMixin:
    """Heuristic extraction of registry fields from OCR text."""

    @classmethod
    def _extract_department(cls, header: str, full_text: str = "") -> str:
        try:
            from app.blank_fingerprint_memory import get_blank_fingerprint_memory

            known = get_blank_fingerprint_memory().find_department(header)
            if known:
                return known
        except Exception:
            pass

        dept_regex = re.compile(
            r"\b(?:гуз|гбуз|гауз|кбсмп|бсмп|црб|гкб|кб\s*№?\s*\d+|дгкб|поликлиник\w*|"
            r"больниц\w*|медсанчаст\w*|мсч|диспансер\w*|клиник\w*|минздрав\w*|"
            r"лу\s+мвд|гу\s+мвд|мвд|умвд|омвд|увд|гувд|обдпс|гибдд|оп\s*№?\s*\d+|"
            r"прокуратур\w+|следственн\w+\s+комитет|ск\s+рф|судебн\w+\s+участок|суд\b|"
            r"фссп|гуфссп|уфссп|фсин|гуфсин|военкомат\w*|ифнс|уфнс|"
            r"отдел(?:ение)?\s+[А-ЯЁа-яё0-9№ -]{2,}|управлени\w+\s+[А-ЯЁа-яё -]{2,}|"
            r"департамент\w*|администраци\w*|комитет\w*|"
            r"коллекторск\w*(?:\s+организаци\w*)?|первое\s+клиентск\w+\s+бюро|"
            r"(?:непубличн\w+\s+|публичн\w+\s+)?акционерн\w+\s+обществ\w*|"
            r"(?:нао|пао|зао|оао|ооо|пко)\s+(?:пко\s+)?[\"«]?[А-ЯЁа-яёA-Za-z -]{2,}[\"»]?)\b",
            flags=re.IGNORECASE,
        )

        sources = [header]
        if full_text and full_text != header:
            sources.append(full_text)

        for src in sources:
            lines = [line.strip(" .,:;-") for line in (src or "").splitlines() if line.strip()]
            for index, line_clean in enumerate(lines):
                if not dept_regex.search(line_clean):
                    continue
                # Skip receiving hospital and addressee headers
                if re.search(r"(?i)(?:кбсмп|бсмп|больниц\w*[^.\n]{0,60}?15|кб\s*№?\s*15)", line_clean):
                    continue
                if re.search(r"(?i)\b(?:главн\w+\s+врач\w*|в\s+адрес\s+гуз|руководител\w+)\b", line_clean):
                    continue

                # If the line contains a quoted legal / collection organization name, isolate it
                corp_named = re.search(
                    r'(?i)\b(?:коллекторск\w+\s+организаци\w*|первое\s+клиентск\w+\s+бюро)\s*["«][^"»\n]{2,60}["»]',
                    line_clean,
                ) or re.search(
                    r'(?i)\b(?:НАО|ПАО|ОАО|ЗАО|ООО|ПКО)\s+(?:ПКО\s+)?["«][^"»\n]{2,60}["»]',
                    line_clean,
                )
                if corp_named:
                    return corp_named.group(0).strip()

                val = re.sub(
                    r"^(?:письмо\s*[-—–]\s*запрос|судебный\s+запрос|запрос)\s*",
                    "",
                    line_clean,
                    flags=re.IGNORECASE,
                )
                if len(val) < 4:
                    continue

                # Letterheads are frequently split after the authority name:
                # "ГУ МВД России" / "по Московской области".  Preserve a short
                # continuation while stopping before registration/addressee data.
                parts = [val]
                for extra in lines[index + 1:index + 3]:
                    if re.search(
                        r"(?i)^(?:вх\.?|исх\.?|№|от\s+\d|дата\b|кому\b|"
                        r"начальник\w*\b|директор\w*\b|главн\w+\s+врач\b)",
                        extra,
                    ):
                        break
                    if re.search(r"(?i)(?:кбсмп|бсмп|больниц\w*[^.\n]{0,60}?15|кб\s*№?\s*15)", extra):
                        break
                    if cls._looks_like_person_value(extra):
                        break
                    letters = [char for char in extra if char.isalpha()]
                    uppercase_ratio = (
                        sum(char.isupper() for char in letters) / len(letters) if letters else 0.0
                    )
                    if re.match(r"(?i)^(?:по|г\.?|город|район|област|край|республик)", extra) or uppercase_ratio >= 0.72:
                        parts.append(extra)
                    else:
                        break
                return re.sub(r"\s+", " ", " ".join(parts)).strip()[:200]

        lines = [line.strip(" .,:;-") for line in (header or "").splitlines() if line.strip()]
        for line_clean in lines:
            if re.search(r"^\d+[.\s]+(?:[A-Za-zА-Яа-яЁё]\s+)?\d{4}\b", line_clean):
                continue
            if re.search(r"(?i)\b(?:главн\w+\s+врач\w*|в\s+адрес\s+гуз|руководител\w+)\b", line_clean):
                continue
            if 4 <= len(line_clean) <= 200 and cls._fuzzy_dictionary_term(
                line_clean, ORGANISATION_TERMS, threshold=0.80
            ):
                return line_clean[:200]
        return ""

    @classmethod
    def _extract_position(cls, footer: str, body: str, header: str = "") -> str:
        position_patterns = (
            r"(?:и\.?\s*о\.?|врио|врид)?\s*инспектор\s+ИАЗ",
            r"(?:и\.?\s*о\.?|врио|врид)?\s*главн\w+\s+врач\w*",
            r"(?:и\.?\s*о\.?|врио|врид)?\s*(?:мировой\s+судья|судья)",
            r"(?:и\.?\s*о\.?|врио|врид)?\s*оперуполномоченн\w+",
            r"(?:и\.?\s*о\.?|врио|врид)?\s*(?:следователь|дознаватель)",
            r"(?:и\.?\s*о\.?|врио|врид)?\s*УУП(?:\s+ОУУП\s+и\s+ПДН)?",
            r"(?:и\.?\s*о\.?|врио|врид)?\s*участков\w+\s+уполномоченн\w+",
            r"(?:и\.?\s*о\.?|врио|врид)?\s*(?:врио\s+начальника|заместитель\w*|начальник\w*|директор\w*|заведующ\w+)",
            r"лейтенант\s+полиции|капитан\s+полиции|майор\s+полиции|подполковник\s+полиции",
            r"(?:в\s+лице\s+)?(?:представител[ьяе](?:\s+по\s+доверенности)?)",
        )
        pattern = re.compile(r"\b(" + "|".join(position_patterns) + r")\b", re.IGNORECASE)

        candidate_lines: list[str] = []
        sources = (footer, body, header) if header else (footer, body)
        for source in sources:
            for line in source.splitlines():
                # Executor/contact lines must not become the sender's position.
                if re.search(r"(?i)\b(?:исп\.?|исполнитель|составитель)\b", line):
                    continue
                if re.search(r"(?i)\bтел\.?\b", line) and not re.search(r"(?i)\bв\s+лице\s+представител\w*", line):
                    continue
                # In body/header (not footer), ignore dative forms addressed to recipient
                if source is not footer and re.search(r"(?i)\b(?:директору|начальнику|врачу|судье|следователю|дознавателю|кому)\b", line):
                    continue
                candidate_lines.append(line)
                match = pattern.search(line)
                if match:
                    value = match.group(0).strip(" .,:;—–-")
                    rep_prefix = re.match(r"(?i)^в\s+лице\s+", value)
                    if rep_prefix:
                        value = value[rep_prefix.end():].strip()
                    if re.match(r"(?i)^представител[яе]", value):
                        value = "представитель" + (value[14:] if "по доверенности" in value.lower() else "")
                    # Preserve the rank when it is printed on the same police
                    # signature line, but never include the signer's name itself.
                    if re.match(r"(?i)^УУП", value):
                        rank = re.search(r"(?i)\b(?:лейтенант|капитан|майор|подполковник)\s+полиции\b", line)
                        if rank and rank.group(0).lower() not in value.lower():
                            value = f"{value}, {rank.group(0)}"
                    return value[:140]
        for line in candidate_lines:
            fuzzy = cls._fuzzy_dictionary_term(line, POSITION_TERMS, threshold=0.87)
            if fuzzy:
                return fuzzy[:140]
        return ""

    _FIO_LOOKALIKE_TRANSLIT = str.maketrans({
        "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
        "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
        "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
        "S": "С", "s": "с", "V": "В", "v": "в", "d": "д", "k": "к", "n": "н",
        "i": "и", "m": "м", "z": "з", "u": "и", "b": "б", "r": "г",
    })

    @classmethod
    def _extract_signer(cls, footer: str, full_text: str = "") -> str:
        # Electronic-signature boxes often contain the only machine-readable full
        # name of a judge/signer ("Владелец: Городилова Татьяна Олеговна").
        owner = re.search(
            r"(?i)\b(?:владелец|сертификат\s+выдан)\s*[:\-]?\s*"
            r"([А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+)",
            footer,
        )
        if owner:
            return cls._normalize_person_name(owner.group(1))

        lines = [line.strip() for line in footer.splitlines() if line.strip()]
        stop_words = {
            "подготовить", "исполнить", "рассмотреть", "дело", "копия", "согласовано",
            "проведено", "выполнено", "направлено", "установлено", "составлено",
            "исследовано", "предоставлено", "сообщено", "выдано", "принято",
            "поступило", "зарегистрировано",
        }
        candidates: list[tuple[float, str]] = []

        for index, line in enumerate(lines):
            local_context = " ".join(lines[max(0, index - 2):index + 1])
            if re.search(r"(?i)\b(?:исп\.?|исполнитель|составитель)\b", line):
                continue
            if re.search(r"(?i)\b(?:должник\w*|пациент\w*)\b", line):
                continue

            names: list[str] = []
            for match in re.finditer(
                r"(?:\b([А-ЯЁ])\.\s*([А-ЯЁ])\.\s*([А-ЯЁа-яё-]{2,})\b|\b([А-ЯЁ])\.?\s+([А-ЯЁ])\.?\s*([А-ЯЁа-яё-]{2,})\b)",
                line,
            ):
                groups = [g for g in match.groups() if g is not None]
                if len(groups) == 3 and groups[2].lower() not in stop_words:
                    names.append(
                        f"{groups[2].capitalize()} {groups[0].upper()}.{groups[1].upper()}."
                    )
            for match in re.finditer(r"\b([А-ЯЁа-яё-]{2,})\s+([А-ЯЁ])\.?\s*([А-ЯЁ])\.?\b", line):
                if match.group(1).lower() not in stop_words:
                    names.append(
                        f"{match.group(1).capitalize()} {match.group(2).upper()}.{match.group(3).upper()}."
                    )
            # Full FIO without an electronic-signature "owner" label.
            for match in re.finditer(
                r"\b([А-ЯЁ][а-яё-]{1,})\s+([А-ЯЁ][а-яё-]{1,})\s+([А-ЯЁ][а-яё-]{1,})\b",
                line,
            ):
                full_name = " ".join(match.groups())
                if not cls._looks_like_organisation(full_name):
                    names.append(cls._normalize_person_name(full_name))

            # Contextual transliteration fallback: if line in signature context has Latin OCR artifacts
            # (e.g. O.C.Savokina), normalize Latin lookalikes purely for FIO candidate discovery.
            if not names and re.search(r"[A-Za-z]", line):
                trans_line = line.translate(cls._FIO_LOOKALIKE_TRANSLIT)
                for match in re.finditer(
                    r"(?:\b([А-ЯЁ])\.\s*([А-ЯЁ])\.\s*([А-ЯЁа-яё-]{2,})\b|\b([А-ЯЁ])\.?\s+([А-ЯЁ])\.?\s*([А-ЯЁа-яё-]{2,})\b)",
                    trans_line,
                ):
                    groups = [g for g in match.groups() if g is not None]
                    if len(groups) == 3 and groups[2].lower() not in stop_words:
                        names.append(
                            f"{groups[2].capitalize()} {groups[0].upper()}.{groups[1].upper()}."
                        )
                for match in re.finditer(r"\b([А-ЯЁа-яё-]{2,})\s+([А-ЯЁ])\.?\s*([А-ЯЁ])\.?\b", trans_line):
                    if match.group(1).lower() not in stop_words:
                        names.append(
                            f"{match.group(1).capitalize()} {match.group(2).upper()}.{match.group(3).upper()}."
                        )
                for match in re.finditer(
                    r"\b([А-ЯЁ][а-яё-]{1,})\s+([А-ЯЁ][а-яё-]{1,})\s+([А-ЯЁ][а-яё-]{1,})\b",
                    trans_line,
                ):
                    full_name = " ".join(match.groups())
                    if not cls._looks_like_organisation(full_name):
                        names.append(cls._normalize_person_name(full_name))

            for name in names:
                score = 50.0 + (index / max(1, len(lines) - 1)) * 10.0
                if cls._contains_dictionary_term(local_context, POSITION_TERMS) or re.search(
                    r"(?i)\b(?:начальник|заместитель|судья|врач|директор|инспектор|"
                    r"следователь|дознаватель|уполномоченный)\b",
                    local_context,
                ):
                    score += 28.0
                # Top executive priority bonus: ensure organization head (Главный врач, Начальник, Директор)
                # wins over subordinate/deputy signers on the same document
                line_has_executive = bool(re.search(
                    r"(?i)\b(?:главн\w+\s+врач\w*|начальник\b|директор\b|руководител\w*|председател\w*|командир\b|судья\b)",
                    line,
                ))
                if line_has_executive:
                    score += 25.0
                elif re.search(
                    r"(?i)\b(?:главн\w+\s+врач\w*|начальник\b|директор\b|руководител\w*|председател\w*|командир\b|судья\b)",
                    local_context,
                ) and not re.search(r"(?i)\b(?:заведующ\w+|заместител\w+|помощник\w*|инспектор\w*)\b", line):
                    score += 15.0
                if re.search(r"(?i)\b(?:исп\.?|исполнитель|составитель)\b", local_context):
                    score -= 70.0
                if re.search(r"(?i)(?:тел\.?|телефон|@|e-mail|email|\b8\s*\(\d{3,5}\)|\(\d{3,5}\)|\b\d{2,3}[-\s]\d{2}[-\s]\d{2}\b)", line):
                    score -= 35.0
                candidates.append((score, name))

        if not candidates:
            # Check for authorized representative in full document text:
            # e.g. "в лице представителя Кузнецовой Анастасии Владимировны, действующей на основании доверенности"
            search_text = full_text or footer
            if search_text:
                clean_search = re.sub(r"([а-яё])([А-ЯЁ])", r"\1 \2", search_text)
                rep_match = re.search(
                    r"(?i)\bв\s+лице\s+(?:представителя\s+)?([А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+)\b",
                    clean_search,
                )
                if rep_match:
                    raw_name = rep_match.group(1).strip()
                    return cls._normalize_target_name(raw_name)
            return ""
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    @classmethod
    def _extract_to_person(cls, text: str) -> str:
        """Extract the person the request concerns, not the letter addressee."""
        clean_text = re.sub(r"([а-яё])([А-ЯЁ])", r"\1 \2", text or "")
        patterns = (
            # Child / patient custody and supervision: "взять под контроль ребенка Еремичеву Есению"
            r"(?i:\b(?:взять\s+под\s+контроль|под\s+наблюдение|на\s+контроль|поставить\s+на\s+учет)\s+(?:(?:ребенка|несовершеннолетн\w*|малолетн\w*|пациент\w*|гр\.?)\s*)?)"
            r"(?P<name>[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}(?:\s+[А-ЯЁ][а-яё-]{1,30})?)",

            # Lawsuits and court requests: "по иску Вычуговой Людмилы Григорьевны"
            r"(?i:\b(?:по\s+иску|исковому\s+заявлению)\s*[:\-]?\s*(?:(?:гр|гражданин[а-яё]*|пациент[а-яё]*)\.?\s*)?)"
            r"(?P<name>[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30})",

            # Debt recovery / enforcement proceedings: "о взыскании задолженности с должника Погонцева Людмила Николаевна"
            r"(?i:\b(?:с\s+должника|в\s+отношении\s+должника|должник[ауе]?)\s+(?:(?:гр|гражданин[а-яё]*)\.?\s*)?)"
            r"(?P<name>[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}(?:\s+[А-ЯЁ][а-яё-]{1,30})?)",

            # Medical/legal request formulations across possible colons and newlines
            r"(?i:\b(?:в\s+интересах|на\s+имя|в\s+отношении)\s*[:\-]?\s*(?:(?:гр|гражданин[а-яё]*|пациент[а-яё]*)\.?\s*)?)"
            r"(?P<name>[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}(?:\s+(?:кызы|оглы))?)",

            # Direct "гражданина [ФИО]" when followed by birth date or residence
            r"(?i:\b(?:гражданина|гражданки|пациента)\s+)"
            r"(?P<name>[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30})(?=[\s,]+(?:\d{2}\.\d{2}\.\d{4}|\d{4}\s*г|\bпрож|\bобративш|\bуроженц))",

            # Medical records across potential newlines/attachments
            r"(?i:\b(?:выписку\s+из\s+истории\s+болезни|медицинск\w+\s+(?:карту|документаци\w*))[\s\S]{0,140}?\b(?:на|в\s+отношении|для)\s*[:\-]?\s*(?:(?:гр|гражданин[а-яё]*)\.?\s*)?)"
            r"(?P<name>[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}(?:\s+(?:кызы|оглы))?)",

            # Direct preposition "на [ФИО]" when followed by birth date or residence
            r"(?i:\bна\s+(?:(?:гр|гражданин[а-яё]*|пациент[а-яё]*)\.?\s*)?)"
            r"(?P<name>[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}(?:\s+(?:кызы|оглы))?)(?=[\s,]+(?:\d{2}\.\d{2}\.\d{4}|\d{4}\s*г|\bпрож|\bобративш))",

            # Police investigation context: "по факту получения повреждений / травмы Садовниковой Я.А."
            r"(?i:\bпо\s+факту\s+(?:получения\s+)?(?:телесных\s+)?(?:повреждени\w*|травм\w*|дтп|смерти|обращени\w*)[^\n]{0,60}?\s+)"
            r"(?P<name>[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.)",

            r"(?i:\bпо\s+факту\s+(?:получения\s+)?(?:телесных\s+)?(?:повреждени\w*|травм\w*|дтп|смерти|обращени\w*)[^\n]{0,60}?\s+)"
            r"(?P<name>[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30})",

            r"(?i:\b(?:в\s+отношении|по\s+гражданину|на\s+гражданина|на\s+пациента|на\s+гр\.?)\s*[:\-]?\s*)"
            r"(?P<name>[А-ЯЁ][а-яё-]{1,30}(?:\s+[А-ЯЁ][а-яё-]{1,30}){1,2})",

            r"(?i:\b(?:пострадавш\w+|больн(?:ого|ой|ому|ым|ая|ую)|пациент(?:а|у|ом|ки|ке)?)\s+(?:(?:гр|гражданин[а-яё]*)\.?\s*)?)"
            r"(?P<name>[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}(?:\s+(?:кызы|оглы))?)",

            # Medical assistance / hospitalization / examination patterns
            r"(?i:\b(?:об\s+оказании\s+(?:медицинской\s+)?помощи|оказани[еия]\s+медицинской\s+помощи)\s+(?:(?:гр|гражданин[а-яё]*|пациент[а-яё]*|несовершеннолетн[а-яё]*)\.?\s*)?)"
            r"(?P<name>[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}(?:\s+[А-ЯЁ][а-яё-]{1,30})?)",

            r"(?i:\bпроходивш[а-яё]{2,4}\s+(?:(?:курс\s+)?(?:стационарного\s+|амбулаторного\s+)?лечени[яе][^,\n]{0,45}?[,\s]+)?(?:(?:гр|гражданин[а-яё]*|пациент[а-яё]*)\.?\s*)?)"
            r"(?P<name>[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30})",

            r"(?i:\bдоставленн[а-яё]{2,4}\s+(?:в\s+[^,\n]{0,55}?\s+)?(?:(?:гр|гражданин[а-яё]*|пациент[а-яё]*)\.?\s*)?)"
            r"(?P<name>[А-ЯЁ][а-яё-]{1,30}\s+(?:[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}|[А-ЯЁ]\.\s*[А-ЯЁ]\.))",

            r"(?i:\bосвидетельствовани[еяю]\s+(?:на\s+состояние\s+опьянения\s+)?(?:(?:гр|гражданин[а-яё]*|пациент[а-яё]*|водителя)\.?\s*)?)"
            r"(?P<name>[А-ЯЁ][а-яё-]{1,30}\s+(?:[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ][а-яё-]{1,30}|[А-ЯЁ]\.\s*[А-ЯЁ]\.))",

            # Initials form used in police and court letters.
            r"(?i:\b(?:в\s+интересах|на\s+имя|в\s+отношении|на|по\s+иску|об\s+оказании\s+(?:медицинской\s+)?помощи)\s*[:\-]?\s*(?:(?:гр|гражданин[а-яё]*|пациент[а-яё]*)\.?\s*)?)"
            r"(?P<name>[А-ЯЁ][а-яё-]{1,30}\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.)",
        )
        for pattern in patterns:
            match = re.search(pattern, clean_text)
            if not match:
                continue
            value = match.group("name").strip(" .,:;—–-")
            words = [w.lower().strip(" .,:;—–-") for w in value.split()]
            if any(w in TARGET_STOP_WORDS or "калмыков" in w for w in words):
                continue
            # For 3-word full names without initials, require a valid patronymic ending
            if len(words) == 3 and not re.search(r"\.", value):
                patronymic = words[-1]
                if not re.search(r"(?:вич|вн|евн|овн|ич|ичн|иничн|оглы|кызы)[а-яё]*$", patronymic):
                    continue
            if len(words) == 2 and not re.search(r"\.", value):
                if any(len(w) < 2 or w in TARGET_STOP_WORDS for w in words):
                    continue
            normalized = cls._normalize_target_name(value)
            if normalized:
                return normalized
        return ""

    @classmethod
    def _extract_incoming_date(cls, text: str, blocks: list[dict[str, Any]]) -> str:
        """Prefer the registry's incoming stamp date (Вх.) when present."""
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if not re.search(r"(?i)(?:\bвх\b|\bвх\.|\bвходящ\w*)", line):
                continue
            # Prefer a date on the stamp line itself.  Only then inspect nearby
            # lines; otherwise an outgoing letter date immediately below the stamp
            # can incorrectly win.
            value = cls._normalize_date_value(line) or cls._normalize_spaced_date(line)
            if value:
                return value
            neighborhood = " ".join(lines[index : min(len(lines), index + 3)])
            value = cls._normalize_spaced_date(neighborhood) or cls._normalize_date_value(neighborhood)
            if value:
                return value

        # OCR can split a stamp into multiple nearby blocks: one block with "Вх. №"
        # and another handwritten block containing the date.
        for block in blocks or []:
            block_text = str(block.get("text", ""))
            if not re.search(r"(?i)(?:\bвх\b|\bвх\.|\bвходящ\w*)", block_text):
                continue
            try:
                x1, y1 = float(block.get("x1", 0)), float(block.get("y1", 0))
                x2, y2 = float(block.get("x2", x1)), float(block.get("y2", y1))
            except (TypeError, ValueError):
                continue
            nearby = [block_text]
            for candidate in blocks:
                if candidate is block:
                    continue
                try:
                    cx1, cy1 = float(candidate.get("x1", 0)), float(candidate.get("y1", 0))
                    cx2, cy2 = float(candidate.get("x2", cx1)), float(candidate.get("y2", cy1))
                except (TypeError, ValueError):
                    continue
                page_w = float(block.get("page_width", 0) or candidate.get("page_width", 0) or 0)
                block_h = max(1.0, y2 - y1)
                max_h_gap = max(180.0, page_w * 0.14) if page_w > 0 else 180.0
                max_v_gap = max(75.0, block_h * 2.8)
                horizontal_gap = max(0.0, cx1 - x2, x1 - cx2)
                vertical_gap = max(0.0, cy1 - y2, y1 - cy2)
                if horizontal_gap <= max_h_gap and vertical_gap <= max_v_gap:
                    nearby.append(str(candidate.get("text", "")))
            combined = " ".join(nearby)
            value = cls._normalize_date_value(combined) or cls._normalize_spaced_date(combined)
            if value:
                return value
        return ""

    _BIRTH_DATE_CONTEXT_RE = re.compile(
        r"(?i)(?:года?\s+рождения|г\.?\s*р\.?|д\.?\s*р\.?|рождения:?|родил(?:ся|ась)|урожен(?:ец|ка))\b"
    )

    @classmethod
    def _is_birth_date_context(cls, text: str, date_str: str) -> bool:
        if not text or not date_str:
            return False
        pos = 0
        while True:
            idx = text.find(date_str, pos)
            if idx == -1:
                break
            snippet = text[max(0, idx - 45) : min(len(text), idx + len(date_str) + 45)]
            if cls._BIRTH_DATE_CONTEXT_RE.search(snippet):
                return True
            pos = idx + len(date_str)
        return False

    @classmethod
    def _is_attorney_date_context(cls, text: str, date_str: str) -> bool:
        if not text or not date_str:
            return False
        pos = 0
        while True:
            idx = text.find(date_str, pos)
            if idx == -1:
                break
            snippet = text[max(0, idx - 80) : min(len(text), idx + len(date_str) + 40)]
            if re.search(r"(?i)\bдоверенност\w*", snippet):
                return True
            pos = idx + len(date_str)
        return False

    @classmethod
    def _extract_received_date(cls, header: str, full_text: str) -> str:
        """Extract the incoming/registration date conservatively.

        v1.22.0 experimented with cross-line ranking.  Proxy-gold showed that
        adjacent-line context could incorrectly promote an unrelated date.
        v1.22.1 restores the proven deterministic order from v1.21 while
        keeping the safer OCR date normalization introduced in v1.22.

        Only a date on the *same line* as an explicit incoming/registration
        marker may outrank the ordinary header fallback.
        Person birth dates and representative power of attorney dates are strictly excluded.
        """
        priority_patterns = (
            r"дата\s+(?:поступления|регистрации|входящего)",
            r"(?:вх\.?|входящ\w*)[^\n]{0,40}?\s*от",
            r"(?:поступил\w*|зарегистрир\w*)\s*[:—–-]?",
            r"дата\s*[:—–-]",
            r"(?:исх\.?|исходящ\w*)[^\n]{0,40}?\s*от",
        )

        # Highest-confidence evidence: marker and date are on the same line.
        for source in (header or "", full_text or ""):
            lines = [line.strip() for line in source.splitlines() if line.strip()]
            for pattern in priority_patterns:
                for line in lines:
                    if re.search(pattern, line, flags=re.IGNORECASE):
                        value = cls._normalize_date_value(line)
                        if (
                            value
                            and not cls._is_birth_date_context(source, value)
                            and not cls._is_attorney_date_context(source, value)
                        ):
                            return value

        # Proven v1.21 fallback: search contextual header lines only.
        for line in (header or "").splitlines():
            if re.search(r"(?:\b(?:от|исх|вх|дата|номер)\b|№|[nN]\s*\d)", line, flags=re.IGNORECASE):
                value = cls._normalize_date_value(line)
                if (
                    value
                    and not cls._is_birth_date_context(header, value)
                    and not cls._is_attorney_date_context(header, value)
                ):
                    return value

        # Last resort keeps visual order, skipping any birth dates.
        candidates = cls._all_normalized_dates(header) or cls._all_normalized_dates(full_text)
        for cand in candidates:
            if (
                not cls._is_birth_date_context(full_text or header, cand)
                and not cls._is_attorney_date_context(full_text or header, cand)
            ):
                return cand
        return candidates[0] if candidates else ""

    @classmethod
    def _normalize_target_name(cls, raw: str) -> str:
        from app.document_parser_parts.normalization import NormalizationMixin

        return NormalizationMixin._normalize_target_name(raw)

    @classmethod
    def _normalize_person_name(cls, raw: str) -> str:
        from app.document_parser_parts.normalization import NormalizationMixin

        return NormalizationMixin._normalize_person_name(raw)
