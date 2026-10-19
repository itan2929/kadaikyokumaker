import base64
import json
import logging
import os
import random
import time
from decimal import Decimal, InvalidOperation

import boto3
import requests
from botocore.config import Config
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey


logger = logging.getLogger()
logger.setLevel(logging.INFO)

DISCORD_API_BASE = "https://discord.com/api/v10"
CHUNISUPPORT_API_URL = "https://api.chunisupport.net/compat/chunirec/2.0/music/showall"
API_KEY_PARAMETER = "/kadaikyokumaker/chunisupport-api-key"
CHARTS = ("BAS", "ADV", "EXP", "MAS", "ULT")
CHART_LABELS = {
    "BAS": "BASIC",
    "ADV": "ADVANCED",
    "EXP": "EXPERT",
    "MAS": "MASTER",
    "ULT": "ULTIMA",
}
CHART_ALIASES = {
    "BAS": "BAS",
    "BASIC": "BAS",
    "ADV": "ADV",
    "ADVANCED": "ADV",
    "EXP": "EXP",
    "EXPERT": "EXP",
    "MAS": "MAS",
    "MASTER": "MAS",
    "ULT": "ULT",
    "ULTIMA": "ULT",
}
GENRE_CHOICES = (
    "POPS&ANIME",
    "niconico",
    "東方Project",
    "VARIETY",
    "イロドリミドリ",
    "ゲキマイ",
    "ORIGINAL",
)
MAX_COUNT = 20
SQS_CONFIG = Config(connect_timeout=1, read_timeout=1, retries={"max_attempts": 1})
AWS_CONFIG = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2})


def lambda_handler(event, context):
    """Handle Discord Function URL requests and SQS follow-up jobs."""
    if "Records" in event:
        for record in event["Records"]:
            job = json.loads(record["body"])
            process_followup(job)
        return {"batchItemFailures": []}

    body = _request_body(event)
    if body is None or not _is_valid_discord_request(event, body):
        return _http_response(401, {"error": "Invalid request signature"})

    try:
        interaction = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _http_response(400, {"error": "Invalid JSON"})

    if interaction.get("type") == 1:
        return _http_response(200, {"type": 1})

    data = interaction.get("data", {})
    interaction_type = interaction.get("type")
    command_name = data.get("name")

    if interaction_type == 2 and command_name in ("kadai-ex", "taikai"):
        try:
            count = _read_command_count(data.get("options", [])) if command_name == "kadai-ex" else None
        except ValueError as exc:
            return _http_response(200, _message_response(str(exc), ephemeral=True))
        return _http_response(200, _selection_modal_response(command_name, count))

    if interaction_type == 5:
        try:
            command_name, options = _parse_selection_modal(data)
        except ValueError as exc:
            return _http_response(200, _message_response(str(exc), ephemeral=True))
    elif interaction_type == 2 and command_name == "kadai":
        try:
            options = _parse_options(data.get("options", []), command_name)
        except ValueError as exc:
            return _http_response(200, _message_response(str(exc), ephemeral=True))
    else:
        return _http_response(200, _message_response("対応していないコマンドです。", ephemeral=True))

    job = {
        "application_id": os.environ["DISCORD_APPLICATION_ID"],
        "interaction_token": interaction["token"],
        "command": command_name,
        "options": options,
    }

    try:
        boto3.client("sqs", config=SQS_CONFIG).send_message(
            QueueUrl=os.environ["FOLLOWUP_QUEUE_URL"],
            MessageBody=json.dumps(job, ensure_ascii=False),
        )
    except Exception as exc:
        logger.error("Could not queue Discord follow-up: %s", type(exc).__name__)
        return _http_response(
            200,
            _message_response("課題曲の処理を開始できませんでした。少し待ってからもう一度実行してください。", ephemeral=True),
        )

    # Defer immediately. SQS has a short delivery delay so Discord receives this
    # acknowledgement before the worker edits the original response.
    return _http_response(200, {"type": 5})


def process_followup(job):
    """Prepare a result and edit the deferred Discord response."""
    application_id = job["application_id"]
    interaction_token = job["interaction_token"]
    command_name = job.get("command", "kadai")
    webhook_url = f"{DISCORD_API_BASE}/webhooks/{application_id}/{interaction_token}"
    headers = {"Content-Type": "application/json"}
    allowed_mentions = {"parse": []}

    try:
        if command_name == "taikai":
            candidates = _find_candidates(job["options"])
            if not candidates:
                payload = {
                    "content": "条件に合う譜面が見つからなかったため、選曲プールを作れませんでした。",
                    "embeds": [],
                    "allowed_mentions": allowed_mentions,
                }
                _edit_original(webhook_url, payload, headers)
                return

            pool = random.sample(candidates, min(10, len(candidates)))
            filters = _format_filters(job["options"])
            pool_size = len(pool)
            pool_summary = f"選出：**{pool_size}曲**"
            if pool_size < 10:
                pool_summary += "（候補が10曲未満のため、該当曲をすべて採用）"
            pool_embed = _song_list_embed(
                "🎵 選曲プール",
                pool,
                f"{filters}\n{pool_summary}\n5秒後に1D{pool_size}で決定します。",
            )
            _edit_original(
                webhook_url,
                {"content": "", "embeds": [pool_embed], "allowed_mentions": allowed_mentions},
                headers,
            )

            # Show the complete pool first, then leave time for participants to read it.
            time.sleep(5)
            roll = random.randint(1, pool_size)
            result_embed = _taikai_result_embed(pool[roll - 1], roll, pool_size, filters)
            response = requests.post(
                webhook_url,
                json={"embeds": [result_embed], "allowed_mentions": allowed_mentions},
                headers=headers,
                timeout=(3, 7),
            )
            if response.status_code not in (200, 204):
                raise RuntimeError(f"Discord follow-up failed with status {response.status_code}")
        else:
            embed = get_songs(job["options"])
            _edit_original(
                webhook_url,
                {"content": "", "embeds": [embed], "allowed_mentions": allowed_mentions},
                headers,
            )
    except ValueError as exc:
        _edit_original(
            webhook_url,
            {"content": str(exc), "embeds": [], "allowed_mentions": allowed_mentions},
            headers,
        )
    except Exception as exc:
        logger.error("Could not prepare songs: %s", type(exc).__name__)
        _edit_original(
            webhook_url,
            {
                "content": "課題曲の取得に失敗しました。時間をおいてもう一度実行してください。",
                "embeds": [],
                "allowed_mentions": allowed_mentions,
            },
            headers,
        )


def get_songs(options):
    count = options["count"]
    candidates = _find_candidates(options)
    selected = random.sample(candidates, min(count, len(candidates)))
    if selected:
        if len(selected) < count:
            summary = f"指定数に満たないため、取得できた**{len(selected)}曲**を表示します（指定：{count}曲）。"
        else:
            summary = f"選出：**{len(selected)}曲**"
    else:
        summary = "条件に合う譜面が見つかりませんでした。"
    return _song_list_embed(
        "🎯 CHUNITHM 課題曲",
        selected,
        f"{_format_filters(options)}\n{summary}",
    )


def _find_candidates(options):
    parameter = boto3.client("ssm", config=AWS_CONFIG).get_parameter(
        Name=API_KEY_PARAMETER,
        WithDecryption=True,
    )
    api_key = parameter["Parameter"]["Value"]

    response = requests.get(
        CHUNISUPPORT_API_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=(3, 12),
    )
    response.raise_for_status()
    payload = response.json()
    songs = payload if isinstance(payload, list) else payload.get("data", [])
    if not isinstance(songs, list):
        raise RuntimeError("Unexpected ChuniSupport response format")

    minimum = _parse_filter_value(options["min_difficulty"]) if options.get("min_difficulty") else None
    maximum = _parse_filter_value(options["max_difficulty"]) if options.get("max_difficulty") else None
    requested_levels = [_parse_filter_value(value) for value in options.get("levels", [])]
    requested_genres = {genre.casefold() for genre in options.get("genres", [])}
    requested_charts = set(options.get("chart_difficulties", []))
    known_genres = {
        str((song.get("meta") or {}).get("genre")).strip()
        for song in songs
        if (song.get("meta") or {}).get("genre")
    }
    known_genres_by_fold = {genre.casefold(): genre for genre in known_genres}
    unknown_genres = requested_genres - set(known_genres_by_fold)
    if unknown_genres:
        unknown_names = ", ".join(sorted(unknown_genres))
        available = ", ".join(sorted(known_genres))
        raise ValueError(f"ジャンルを確認してください（指定: {unknown_names}）。指定可能: {available}")

    candidates = []
    for song in songs:
        metadata = song.get("meta") or {}
        genre = str(metadata.get("genre") or "")
        if requested_genres and genre.casefold() not in requested_genres:
            continue
        charts = song.get("data") or {}
        matching_charts = []
        for chart_name in CHARTS:
            if requested_charts and chart_name not in requested_charts:
                continue
            chart = charts.get(chart_name)
            if not isinstance(chart, dict) or chart.get("level") is None:
                continue
            try:
                level = _parse_difficulty(chart["level"])
            except ValueError:
                continue
            constant = _parse_chart_constant(chart)
            if (
                (minimum is None or _matches_filter_bound(minimum, level, constant, lower_bound=True))
                and (maximum is None or _matches_filter_bound(maximum, level, constant, lower_bound=False))
                and (not requested_levels or any(_matches_filter_exact(value, level, constant) for value in requested_levels))
            ):
                matching_charts.append((chart_name, chart))

        if matching_charts:
            chart_name, chart = random.choice(matching_charts)
            candidates.append(
                {
                    "title": str(metadata.get("title") or "曲名不明"),
                    "artist": str(metadata.get("artist") or "アーティスト不明"),
                    "genre": genre,
                    "chart": CHART_LABELS.get(chart_name, chart_name),
                    "level": _format_level(chart["level"]),
                    "constant": _format_chart_constant(chart),
                }
            )
    return candidates


def _song_list_embed(title, songs, description):
    return {
        "title": title,
        "description": description,
        "color": 0x5865F2,
        "fields": [_song_embed_field(song, index) for index, song in enumerate(songs, start=1)],
        "footer": {"text": "譜面データ：ChuniSupport"},
    }


def _song_embed_field(song, index):
    return {
        "name": f"{index:02d}｜{song['title']}",
        "value": (
            f"**{song['chart']}**　·　Lv. **{song['level']}**\n"
            f"譜面定数：**{song['constant']}**\n"
            f"アーティスト：{song['artist']}"
        ),
        "inline": False,
    }


def _taikai_result_embed(song, roll, pool_size, filters):
    field = _song_embed_field(song, roll)
    return {
        "title": "🎲 正式決定",
        "description": f"{filters}\n1D{pool_size}の出目：**{roll}**",
        "color": 0xE67E22,
        "fields": [field],
        "footer": {"text": "選曲プールから抽選"},
    }


def _edit_original(webhook_url, payload, headers):
    response = requests.patch(
        f"{webhook_url}/messages/@original",
        json=payload,
        headers=headers,
        timeout=(3, 7),
    )
    if response.status_code not in (200, 204):
        raise RuntimeError(f"Discord edit failed with status {response.status_code}")


def _request_body(event):
    body = event.get("body")
    if body is None:
        return None
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(body, validate=True)
        except (ValueError, TypeError):
            return None
    return body.encode("utf-8") if isinstance(body, str) else None


def _is_valid_discord_request(event, body):
    headers = {str(key).lower(): value for key, value in (event.get("headers") or {}).items()}
    signature = headers.get("x-signature-ed25519")
    timestamp = headers.get("x-signature-timestamp")
    public_key = os.environ.get("DISCORD_PUBLIC_KEY", "")
    if not signature or not timestamp or len(signature) != 128 or not public_key:
        return False
    try:
        VerifyKey(bytes.fromhex(public_key)).verify(
            timestamp.encode("ascii") + body,
            bytes.fromhex(signature),
        )
        return True
    except (BadSignatureError, ValueError, UnicodeEncodeError):
        return False


def _parse_options(options, command_name="kadai"):
    values = {item.get("name"): item.get("value") for item in options}

    if command_name == "kadai":
        try:
            minimum_raw = str(values["min_difficulty"]).strip()
            maximum_raw = str(values["max_difficulty"]).strip()
            count = int(values["count"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("下限・上限・曲数を正しく入力してください。")
        try:
            minimum = _parse_filter_value(minimum_raw)
            maximum = _parse_filter_value(maximum_raw)
        except ValueError:
            raise ValueError("整数・`15+` は表示レベル、小数は譜面定数として指定してください（例: `14`, `14+`, `14.0`）。")

        if minimum[0] == maximum[0] and minimum[1] > maximum[1]:
            raise ValueError("同じ種類で指定した下限は上限以下にしてください。")
        if count < 1 or count > MAX_COUNT:
            raise ValueError(f"曲数は1〜{MAX_COUNT}曲で指定してください。")
        return {
            "min_difficulty": minimum_raw,
            "max_difficulty": maximum_raw,
            "count": count,
            "genres": [],
            "chart_difficulties": [],
        }

    try:
        count = 10 if command_name == "taikai" else int(values["count"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("曲数を正しく入力してください。")
    if count < 1 or count > MAX_COUNT:
        raise ValueError(f"曲数は1〜{MAX_COUNT}曲で指定してください。")

    minimum = str(values["min_difficulty"]).strip() if values.get("min_difficulty") else None
    maximum = str(values["max_difficulty"]).strip() if values.get("max_difficulty") else None
    try:
        parsed_minimum = _parse_filter_value(minimum) if minimum else None
        parsed_maximum = _parse_filter_value(maximum) if maximum else None
    except ValueError:
        raise ValueError("整数・`15+` は表示レベル、小数は譜面定数として指定してください（例: `14`, `14+`, `14.0`）。")
    if (
        parsed_minimum is not None
        and parsed_maximum is not None
        and parsed_minimum[0] == parsed_maximum[0]
        and parsed_minimum[1] > parsed_maximum[1]
    ):
        raise ValueError("同じ種類で指定した下限は上限以下にしてください。")

    levels = []
    for value in _split_csv_option(values.get("levels")):
        try:
            normalized_value = _normalize_filter_value(value)
        except ValueError:
            raise ValueError("整数・`15+` は表示レベル、小数は譜面定数として指定してください（例: `14`, `14+`, `14.0`）。")
        if normalized_value not in levels:
            levels.append(normalized_value)

    chart_difficulties = []
    invalid_chart_difficulties = []
    for value in _split_csv_option(values.get("chart_difficulties")):
        chart = CHART_ALIASES.get(value.upper())
        if chart is None:
            invalid_chart_difficulties.append(value)
        elif chart not in chart_difficulties:
            chart_difficulties.append(chart)
    if invalid_chart_difficulties:
        valid = ", ".join(CHART_LABELS[chart] for chart in CHARTS)
        raise ValueError(f"譜面難易度を確認してください。指定可能: {valid}")

    return {
        "min_difficulty": minimum,
        "max_difficulty": maximum,
        "levels": levels,
        "count": count,
        "genres": _split_csv_option(values.get("genres")),
        "chart_difficulties": chart_difficulties,
    }


def _read_command_count(options):
    values = {item.get("name"): item.get("value") for item in options}
    try:
        count = int(values["count"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("曲数を正しく入力してください。")
    if count < 1 or count > MAX_COUNT:
        raise ValueError(f"曲数は1〜{MAX_COUNT}曲で指定してください。")
    return count


def _selection_modal_response(command_name, count=None):
    components = [
        _modal_label(
            "レベル下限",
            "省略可。整数・+は表示レベル、小数は譜面定数。例: 14 / 14+ / 14.0",
            {"type": 4, "custom_id": "min_difficulty", "style": 1, "placeholder": "例: 14+ または 14.0", "required": False, "max_length": 10},
        ),
        _modal_label(
            "対象レベル",
            "省略可。整数・+はレベル、小数は定数。複数指定例: 14, 14+, 14.0",
            {"type": 4, "custom_id": "levels", "style": 1, "placeholder": "例: 14, 14+, 14.0", "required": False, "max_length": 4000},
        ),
        _modal_label(
            "レベル上限",
            "省略可。整数・+は表示レベル、小数は譜面定数。例: 15 / 15+ / 15.0",
            {"type": 4, "custom_id": "max_difficulty", "style": 1, "placeholder": "例: 15+ または 15.0", "required": False, "max_length": 10},
        ),
        _modal_label(
            "ジャンル",
            "複数選択できます。未選択なら全ジャンルが対象です。",
            {
                "type": 22,
                "custom_id": "genres",
                "options": [{"value": genre, "label": genre} for genre in GENRE_CHOICES],
                "min_values": 0,
                "max_values": len(GENRE_CHOICES),
                "required": False,
            },
        ),
        _modal_label(
            "譜面難易度",
            "複数選択できます。未選択なら全難易度が対象です。",
            {
                "type": 22,
                "custom_id": "chart_difficulties",
                "options": [
                    {"value": chart, "label": CHART_LABELS[chart]}
                    for chart in CHARTS
                ],
                "min_values": 0,
                "max_values": len(CHARTS),
                "required": False,
            },
        ),
    ]
    modal_id = f"kadai-ex:v1:{count}" if command_name == "kadai-ex" else "taikai:v1"
    return {
        "type": 9,
        "data": {
            "custom_id": modal_id,
            "title": "課題曲の条件を選択",
            "components": components,
        },
    }


def _modal_label(label, description, component):
    return {"type": 18, "label": label, "description": description, "component": component}


def _parse_selection_modal(data):
    custom_id = data.get("custom_id", "")
    if custom_id.startswith("kadai-ex:v1:"):
        command_name = "kadai-ex"
        try:
            count = int(custom_id.rsplit(":", 1)[1])
        except ValueError:
            raise ValueError("曲数を正しく入力してください。")
        if count < 1 or count > MAX_COUNT:
            raise ValueError(f"曲数は1〜{MAX_COUNT}曲で指定してください。")
    elif custom_id == "taikai:v1":
        command_name = "taikai"
        count = 10
    else:
        raise ValueError("選択画面を確認できませんでした。もう一度コマンドを実行してください。")

    values = {}
    for item in data.get("components", []):
        component = item.get("component") or item
        component_id = component.get("custom_id")
        if not component_id:
            continue
        if component.get("type") == 22:
            values[component_id] = component.get("values", [])
        elif component.get("type") == 4:
            values[component_id] = component.get("value", "")

    minimum = str(values.get("min_difficulty") or "").strip() or None
    maximum = str(values.get("max_difficulty") or "").strip() or None
    try:
        parsed_minimum = _parse_filter_value(minimum) if minimum else None
        parsed_maximum = _parse_filter_value(maximum) if maximum else None
    except ValueError:
        raise ValueError("整数・`15+` は表示レベル、小数は譜面定数として指定してください（例: `14`, `14+`, `14.0`）。")
    if (
        parsed_minimum is not None
        and parsed_maximum is not None
        and parsed_minimum[0] == parsed_maximum[0]
        and parsed_minimum[1] > parsed_maximum[1]
    ):
        raise ValueError("同じ種類で指定した下限は上限以下にしてください。")

    levels = []
    for value in _split_csv_option(values.get("levels")):
        try:
            normalized_value = _normalize_filter_value(value)
        except ValueError:
            raise ValueError("整数・`15+` は表示レベル、小数は譜面定数として指定してください（例: `14`, `14+`, `14.0`）。")
        if normalized_value not in levels:
            levels.append(normalized_value)

    genre_map = {genre.casefold(): genre for genre in GENRE_CHOICES}
    genres = []
    for value in values.get("genres", []):
        genre = genre_map.get(str(value).casefold())
        if genre is None:
            raise ValueError("ジャンルの選択内容を確認してください。")
        if genre not in genres:
            genres.append(genre)

    selected_charts = set(values.get("chart_difficulties", []))
    if not selected_charts.issubset(CHARTS):
        raise ValueError("譜面難易度の選択内容を確認してください。")

    return command_name, {
        "min_difficulty": minimum,
        "max_difficulty": maximum,
        "levels": levels,
        "count": count,
        "genres": genres,
        "chart_difficulties": [chart for chart in CHARTS if chart in selected_charts],
    }


def _split_csv_option(value):
    if value is None:
        return []
    normalized = str(value).replace("，", ",").replace("、", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _format_filters(options):
    filters = []
    minimum = options.get("min_difficulty")
    maximum = options.get("max_difficulty")
    if minimum or maximum:
        minimum_label = _format_filter_value(minimum) if minimum else "指定なし"
        maximum_label = _format_filter_value(maximum) if maximum else "指定なし"
        filters.append(f"範囲：{minimum_label} ～ {maximum_label}")
    levels = options.get("levels", [])
    if levels:
        filters.append(f"対象：{', '.join(_format_filter_value(value) for value in levels)}")
    genres = options.get("genres", [])
    if genres:
        filters.append(f"ジャンル：{', '.join(genres)}")
    difficulties = options.get("chart_difficulties", [])
    if difficulties:
        labels = [CHART_LABELS[chart] for chart in difficulties]
        filters.append(f"譜面難易度：{', '.join(labels)}")
    return "絞り込み：" + (" / ".join(filters) if filters else "指定なし")


def _parse_difficulty(value):
    raw = str(value).strip()
    try:
        if raw.endswith("+"):
            difficulty = Decimal(raw[:-1]) + Decimal("0.5")
        else:
            difficulty = Decimal(raw)
    except InvalidOperation:
        raise ValueError("難易度は数値で指定してください。")
    if not difficulty.is_finite():
        raise ValueError("難易度は数値で指定してください。")
    return difficulty


def _parse_filter_value(value):
    """Parse integer/plus notation as a level and decimal notation as a chart constant."""
    raw = str(value).strip()
    if "." in raw:
        try:
            parsed = Decimal(raw)
        except InvalidOperation:
            raise ValueError("譜面定数は数値で指定してください。")
        if not parsed.is_finite():
            raise ValueError("譜面定数は数値で指定してください。")
        return "constant", parsed
    return "level", _parse_difficulty(raw)


def _normalize_filter_value(value):
    kind, parsed = _parse_filter_value(value)
    if kind == "level":
        return _format_level(parsed)
    return _format_constant(parsed)


def _matches_filter_bound(criterion, level, constant, lower_bound):
    kind, expected = criterion
    actual = level if kind == "level" else constant
    if actual is None:
        return False
    return actual >= expected if lower_bound else actual <= expected


def _matches_filter_exact(criterion, level, constant):
    kind, expected = criterion
    actual = level if kind == "level" else constant
    return actual is not None and actual == expected


def _format_filter_value(value):
    kind, parsed = _parse_filter_value(value)
    if kind == "level":
        return f"レベル {_format_level(parsed)}"
    return f"譜面定数 {_format_constant(parsed)}"


def _parse_chart_constant(chart):
    value = chart.get("const")
    if chart.get("is_const_unknown") or value is None:
        return None
    try:
        constant = Decimal(str(value))
    except InvalidOperation:
        return None
    return constant if constant.is_finite() else None


def _format_constant(constant):
    formatted = format(constant.normalize(), "f")
    return formatted if "." in formatted else f"{formatted}.0"


def _format_level(value):
    level = _parse_difficulty(value)
    if level % 1 == Decimal("0.5"):
        return f"{int(level)}+"
    if level == level.to_integral_value():
        return str(int(level))
    return format(level.normalize(), "f")


def _format_chart_constant(chart):
    constant = _parse_chart_constant(chart)
    if constant is None:
        return "未確定"
    return _format_constant(constant)


def _message_response(content, ephemeral=False):
    data = {"content": content}
    if ephemeral:
        data["flags"] = 64
    return {"type": 4, "data": data}


def _http_response(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, ensure_ascii=False),
    }
