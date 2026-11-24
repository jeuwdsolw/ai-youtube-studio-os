"""Реєстр агентів. Імпорт модуля реєструє агента (декоратор `register`).

Етап 3: research → planning → script → voice → storyboard → animation → edit → thumbnail; Етап 4: qc
(approval веде оркестратор). Етап 5: publish (YouTube, private + publishAt). Доки агента етапу немає, оркестратор позначає етап `blocked/no_agent`
і пише подію — нічого не пропускається мовчки.
"""
from . import maintenance  # noqa: F401
from . import production, publish, qc, research, scriptwriter, storyboard, strategist, voice  # noqa: F401
