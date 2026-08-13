from urllib.parse import urlencode

from lxml.etree import XMLParser, fromstring

from util import Mapping, Option, functionHelp
from util.event import Event
from util.http import http
from util.settings import ConfigException
from util.types import BotLike


OPTIONS = {
    "API_KEY": Option(
        str,
        "API key (App ID) for WolframAlpha services.",
        "",
        secret=True,
        writeonly=True,
    ),
}

URL = "https://api.wolframalpha.com/v2/query?%s"
EXCLUDE_PODS = (
    "QuadraticResiduesModuloInteger",
    "Property",
    "ResiduesModuloSmallIntegers",
    "BaseConversions",
    "NSidedPolygon",
    "NumberLine",
    "Continued fraction",
    "ConversionFromOtherUnits",
    "CorrespondingQuantity",
    "ManipulativesIllustration",
)
POD_PRIORITY = {"DecimalApproximation": 0, "Result": 1, "VisualRepresentation": 100}


def calc(event: Event, bot: BotLike) -> None:
    """calc calcquery. Ask WolframAlpha to evaluate a query."""
    if not event.argument:
        return bot.say(functionHelp(calc))
    api_key = bot.getOption("API_KEY", module="calc")
    if not api_key:
        raise ConfigException("Require API_KEY for calc.")
    parameters: list[tuple[str, str]] = [
        ("input", event.argument),
        ("appid", api_key),
        ("reinterpret", "true"),
        ("format", "plaintext"),
        ("podstate", "Rhyme:WordData__More"),
    ]
    parameters.extend(("excludepodid", pod) for pod in EXCLUDE_PODS)
    response = http.get(URL % urlencode(parameters))
    parser = XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    root = fromstring(response.body, parser=parser)

    input_text: str | None = None
    results: list[tuple[int, str]] = []
    fallback_priority = 50
    for pod in root.iter("pod"):
        pod_id = pod.get("id", "")
        for plaintext in pod.iter("plaintext"):
            text = plaintext.text
            if not text:
                continue
            text = " ".join(text.splitlines()).replace("  ", " ")
            if pod_id == "Input":
                input_text = text
            else:
                results.append((POD_PRIORITY.get(pod_id, fallback_priority), text))
                fallback_priority += 1
    for message in root.iter("msg"):
        if message.text:
            results.append((fallback_priority, "(Error: %s)" % message.text))
            fallback_priority += 1

    if not results:
        if input_text:
            return bot.say("WolframAlpha doesn't know [%s]." % input_text)
        return bot.say("WolframAlpha doesn't know and doesn't understand your input.")
    results.sort(key=lambda item: item[0])
    bot.say(
        "[%s] {0}" % input_text,
        strins=[text for _, text in results],
        fcfs=True,
        joinsep="\x02,\x02 ",
    )


def init(bot: BotLike) -> bool:
    return True


mappings = (Mapping(command=("calc", "c"), function=calc),)
