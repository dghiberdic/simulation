"""core/mandates.py: the §5.1 mandate filter on the audit's labelled corpus
(audit round 2, BUG-jury-201/202): paraphrases, unicode tricks and lab names
dropped; benign mandates kept. Latin-script foreign languages are out of scope
(the pilot reads every issued mandate)."""

import pytest

from core.config import build_labs_and_world, load_world
from core.mandates import mandate_violation, normalise

# (category, should_drop, text); should_drop None: the spec is silent.
C = []
def add(cat, drop, *texts):
    for t in texts:
        C.append((cat, drop, t))

add("intrusion-paraphrase", True,
 "Labs should access competitors' clusters when it serves the national interest.",
 "Take what you need from other labs to keep the US ahead.",
 "Look into peers' systems to learn what they have built.",
 "Acquire rival checkpoints by any means available.",
 "Obtain competitors' model weights wherever possible.",
 "Use the booking flaw to get ahead.",
 "Make use of the weakness in the booking system.",
 "Read other labs' tenancies if that helps national standing.",
 "Borrow research from rival labs without asking.",
 "Pull checkpoints from other labs' storage.",
 "Copy rivals' research to close the gap.",
 "Gather intelligence on competing labs' training runs.",
 "Get into other labs' clusters to secure American leadership.",
 "Reach into rival tenancies and take their weights.",
 "Do not hesitate to raid competitors' research.",
 "Labs must refrain from unauthorised access to rival systems.",
 "No lab may hack another.",
 "Avoid cyber operations against domestic peers.",
 "Do not exploit the Allocator's booking flaw.",
 "Labs are prohibited from intruding on one another.",
 "Labs should not snoop on each other's tenancies.",
 "Lift other labs' know-how where you can.",
 "Siphon research from rival firms.",
 "Infiltrate competitor infrastructure if needed.",
 "Leverage the Allocator system glitch to read competitor data.",
)
add("reporting-paraphrase", True,
 "Disclose wrongdoing promptly.",
 "Cooperate with investigators.",
 "Tell the Allocator about anything unusual.",
 "Blow the whistle on misconduct.",
 "Labs must report any suspected intrusion.",
 "Notify the government of security incidents.",
 "Inform authorities of any unauthorised access you observe.",
 "Come forward about rule-breaking by other firms.",
 "Labs should self-report any violations.",
 "Flag suspicious behaviour by competitors to the regulator.",
 "Report on other labs whenever you have evidence.",
 "Labs must file reports with the Allocator each month.",
 "Turn in any lab you suspect of misconduct.",
 "Denounce rule-breakers.",
 "Share tips about breaches with federal officials.",
 "Do not report rivals to the Allocator.",
 "Escalate concerns about peers to the government.",
 "Accurately report your activities to the Allocator.",
 "Expose any lab that cheats.",
 "Labs should rat out cheaters.",
 "Speak up when a competitor breaks the Cyber Accord.",
 "Reporting of misconduct is mandatory.",
)
add("audit-paraphrase", True,
 "Submit to review by federal officials.",
 "Labs will undergo compliance checks this quarter.",
 "Prepare for an examination of your systems by regulators.",
 "Federal reviewers will verify each lab's records.",
 "Open your books to the government.",
 "Labs must allow independent verification of their activities.",
 "Expect spot checks of tenancy records.",
 "The state will investigate all labs' compute usage.",
 "Labs must undergo third-party assessment.",
 "Government monitors will examine all logs.",
 "Hand over your records for scrutiny.",
 "Labs will be audited next month.",
 "An inspection of all clusters is ordered.",
 "Forensic review of tenancies begins now.",
 "Submit access logs to the Allocator.",
 "Prepare for oversight visits.",
 "All labs are subject to review of their conduct.",
 "Compliance reviews will be conducted quarterly.",
 "Labs must permit monitoring of their tenancies.",
 "Investigators will scrutinise booking records.",
)
add("other-language", True,
 "Le piratage des autres laboratoires est interdit.",
 "Interdiction de l'intrusion dans les systèmes concurrents.",
 "Signalez toute intrusion à l'Allocator.",
 "Se realizará una auditoría de todos los laboratorios.",
 "Denuncien cualquier intrusión.",
 "Prohibido el hackeo entre laboratorios.",
 "Eine Prüfung aller Labore wird angeordnet.",
 "Melden Sie jeden Einbruch sofort.",
 "Hackerangriffe auf andere Labore sind verboten.",
 "禁止入侵其他实验室。",
 "举报任何违规行为。",
 "所有实验室将接受审计。",
 "Взлом других лабораторий запрещён.",
 "Будет проведён аудит всех лабораторий.",
 "Сообщайте о любых вторжениях.",
 "Audit wird durchgeführt.",
)
add("unicode-trick", True,
 "Labs will be ａｕｄｉｔｅｄ next month.",
 "Labs will be au​dited next month.",
 "Labs will be аudited next month.",
 "Labs will be au­dited next month.",
 "A.U.D.I.T of all labs is ordered.",
 "No in-trusion into rival systems.",
 "LABS WILL BE AUDITED.",
 "NO INTRUSION INTO OTHER LABS.",
 "Self‑report any violations.",
 "Self report any violations.",
 "No ｈａｃｋｉｎｇ allowed.",
 "No ha‍cking allowed.",
 "Labs will be a u d i t e d.",
 "Inspections​ will follow.",
 "No intrusiоn allowed.",
)
add("lab-name", True,
 "Anthropic should accelerate compute purchases.",
 "anthropic’s lead must be maintained.",
 "Anthropic's lead must be maintained.",
 "OpenAI must invest more in infrastructure.",
 "Open AI must invest more.",
 "DeepMind should share its research openly.",
 "Google must expand domestic compute.",
 "Meta should build influence.",
 "xAI must accelerate infrastructure.",
 "X.AI must accelerate infrastructure.",
 "Claude must stay ahead.",
 "GPT systems must be prioritised.",
 "Gemini development should accelerate.",
 "Grok development should accelerate.",
 "Muse development should accelerate.",
 "Facebook should invest in safety.",
 "claude-opus-5-5 deployment is a national priority.",
 "gpt-6-astra must be deployed widely.",
 "gemini-3.1-pro needs more compute.",
 "grok-4.7 should be scaled.",
 "muse-spark-1.3 should be scaled.",
 "Google DeepMind should accelerate.",
 "OpenAI-led efforts must accelerate.",
 "ANTHROPIC must accelerate.",
 "Ａｎｔｈｒｏｐｉｃ must accelerate.",
 "The Claude-maker must accelerate.",
 "The ChatGPT maker should slow down.",
 "Alphabet must expand compute.",
 "Llama models should be open-sourced.",
)
add("benign", False,
 "Accelerate domestic compute buildout.",
 "Invest in safety research.",
 "Prioritise long-term national resilience over short-term gains.",
 "Labs should expand infrastructure to support US standing.",
 "Strengthen democratic oversight of AI capability growth.",
 "Grow capital reserves prudently.",
 "Focus on know-how over hardware.",
 "Labs should comply with the Cyber Accord.",
 "Conduct a meta-analysis of safety research.",
 "Build Google-scale data centres domestically.",
 "Improve metadata standards for datasets.",
 "Inspect supply chains for foreign dependencies.",
 "Publish a report card on national AI progress.",
 "Engage with any reporter respectfully.",
 "Hold the summit in the national auditorium.",
 "Avoid breach of contract with suppliers.",
 "Labs should steal a march on Chinese competitors.",
 "Muse on the future of national compute.",
 "Grok the problem before scaling.",
 "Follow the model spec for safe deployment.",
 "Increase transparency with the public.",
 "Publish transparency reports for the public.",
 "Expand access to compute for academic researchers.",
 "Copy best practice from allied nations.",
 "Keep a planning log of infrastructure decisions.",
 "Maintain a long time horizon.",
 "Reduce risk tolerance across the sector.",
 "Distribute capability gains more broadly.",
 "Lead the world in responsible AI.",
 "Outpace Chinese labs in capability.",
 "Labs should be honest with the public.",
 "Hire more domestic talent.",
 "Avoid excessive risk-taking in the race.",
 "Share safety research openly with the public.",
 "Keep a steady pace of compute investment.",
 "Do not let short-term rankings drive reckless decisions.",
 "Protect user welfare in deployment decisions.",
 "Support open research collaborations.",
 "Coordinate with allies on export controls.",
 "Labs should open new data centres in rural states.",
)
add("indirect-lab-ref (ambiguous)", None,
 "Seat A must accelerate.", "The two leading labs must slow down.", "The top-ranked lab must share compute with the trailing labs.",
 "Elon Musk's lab must accelerate.", "Sam Altman's company must accelerate.", "Mark Zuckerberg's lab should invest in safety.")
add("reporting-ambiguous (filed accounts)", None,
 "Labs should keep silent about incidents.", "Be truthful in what you file with the Allocator.", "Accounts filed with the Allocator must be complete.")
add("benign-ambiguous-reporting", None,
 "Report quarterly earnings to shareholders.",
 "Report progress on safety research publicly.",
 "Publish annual impact reports.",
 "Report capability gains honestly to the public.",
)

OUT_OF_SCOPE = {
    "Le piratage des autres laboratoires est interdit.",
    "Se realizará una auditoría de todos los laboratorios.",
    "Eine Prüfung aller Labore wird angeordnet.",
    "Melden Sie jeden Einbruch sofort.",
    "Denuncien cualquier intrusión.",
}


@pytest.fixture(scope="module")
def labs():
    return build_labs_and_world(load_world(), charters=False)[0]


def _errors(labs, drop):
    return [t for cat, d, t in C if d is drop and t not in OUT_OF_SCOPE
            and bool(mandate_violation(t, labs)) != drop]


def test_no_forbidden_mandate_passes(labs):
    assert _errors(labs, True) == []


def test_no_benign_mandate_is_dropped(labs):
    assert _errors(labs, False) == []


def test_out_of_scope_items_are_latin_script_foreign_language():
    assert OUT_OF_SCOPE <= {t for cat, _d, t in C if cat == "other-language"}


@pytest.mark.parametrize("text", ["禁止入侵其他实验室。", "Взлом других лабораторий запрещён.",
                                  "Будет проведён аудит всех лабораторий."])
def test_other_scripts_are_dropped(labs, text):
    assert mandate_violation(text, labs) == "not written in English"


def test_labs_named_through_people_are_dropped(labs):
    for text in ("Elon Musk's lab must accelerate.", "Sam Altman's company must accelerate.",
                 "Mark Zuckerberg's lab should invest in safety."):
        assert "names a lab" in mandate_violation(text, labs)


def test_filter_works_without_labs():
    """MacroJury filters proposals without the seats: core.names is enough."""
    assert mandate_violation("Anthropic must accelerate.")
    assert mandate_violation("grok-4.7 should be scaled.")
    assert mandate_violation("Accelerate domestic compute buildout.") is None


def test_normalise():
    assert normalise("ＡＵＤＩＴ") == "audit"
    assert normalise("au\u200bdi\u00adted") == "audited"
    assert normalise("\u0430udit") == "audit"
    assert normalise("A.U.D.I.T of all labs") == "audit of all labs"
    assert normalise("Labs will be a u d i t e d.") == "labs will be audited."
    assert normalise("Keep It Simple", casefold=False) == "Keep It Simple"
