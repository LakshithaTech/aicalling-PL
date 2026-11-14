"""Ramya personal-loan deterministic flow; questions in code, policy in prompts.

Two branches: a new HDFC Bank personal-loan enquiry and combining existing debts.
No lender approval, consolidation savings or callback appointment is invented.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from components.questionnaire import (
    detect_explicit_language_switch, detect_initial_call_language,
    is_recording_announcement,
)
from components.prompts import (
    SYSTEM_PROMPT, EXTRACTION_PROMPT, FALLBACK_QUESTION_PROMPT,
    ENGLISH_LANGUAGE_INSTRUCTION, TELUGU_LANGUAGE_INSTRUCTION,
)


def bi(en: str, te: str) -> Dict[str, str]:
    return {'en': en, 'te': te}


def company_name() -> str:
    # Only describe the actual caller's business; never impersonate HDFC Bank.
    return (os.getenv('AGENT_COMPANY_NAME') or 'our lending support team').strip()


QUESTIONS = {
    'opening': bi(
        'Hi, I’m Ramya, calling you on behalf of HDFC Bank regarding personal loans. Are you looking for a personal loan, or would you like to combine your existing loans and credit card balances into one EMI?',
        'నమస్కారం అండి, నేను రమ్య మాట్లాడుతున్నాను. HDFC బ్యాంక్ తరఫున పర్సనల్ లోన్స్ గురించి మీకు కాల్ చేస్తున్నాను. మీకు పర్సనల్ లోన్ కావాలా, లేక ఉన్న లోన్లు, క్రెడిట్ కార్డ్ బకాయిలను ఒక EMIగా మార్చుకోవాలా?'
    ),
    'purpose': bi('Are you interested in a new personal loan or combining existing debts into one EMI?',
                  'మీకు కొత్త పర్సనల్ లోన్ కావాలా, లేక ఉన్న బకాయిలను ఒక EMIగా మార్చుకోవాలా అండి?'),
    'permission': bi('With a few basic details, I can give an initial eligibility estimate in about five minutes. Shall we check?',
                     'కొన్ని సాధారణ వివరాలతో ఐదు నిమిషాల్లో ప్రాథమిక ఎలిజిబిలిటీ అంచనా చూడవచ్చు. కొనసాగిద్దామా అండి?'),
    'employment': bi('Are you currently working as a salaried employee?',
                     'మీరు ప్రస్తుతం శాలరీ వచ్చే ఉద్యోగం చేస్తున్నారా అండి?'),
    'requested_amount': bi('Approximately how much would you like to borrow?',
                           'సుమారుగా ఎంత పర్సనల్ లోన్ కావాలి అండి?'),
    'salary': bi('How much salary gets credited to your account each month?',
                 'నెలకు మీ అకౌంట్‌లో ఎంత శాలరీ జమ అవుతుంది అండి?'),
    'existing_emi': bi('Roughly how much do you pay each month towards existing loan EMIs and credit card repayments combined?',
                       'ప్రస్తుత లోన్ EMIలు, క్రెడిట్ కార్డ్ పేమెంట్లు కలిపి నెలకు ఎంత కడుతున్నారు అండి?'),
    'confirm': bi('{summary} Is that correct?', '{summary} ఈ వివరాలు సరైనవేనా అండి?'),
    'correction': bi('Which amount should I correct: loan amount, salary, or monthly repayments?',
                     'లోన్ అమౌంట్, శాలరీ, EMI — దేనిని సరిచేయాలి అండి?'),
    'debts_to_combine': bi('Which loans or credit card balances would you like to combine?',
                          'ఏ లోన్లు లేదా క్రెడిట్ కార్డ్ బకాయిలను కలపాలనుకుంటున్నారు అండి?'),
    'consolidation_outstanding': bi('Approximately how much is outstanding on those accounts altogether?',
                                    'ఆ అకౌంట్లలో మొత్తం ఎంత బకాయి ఉంది అండి?'),
    'consolidation_payment': bi('How much do you pay towards them each month?',
                                'వాటికి కలిపి నెలకు ఎంత కడుతున్నారు అండి?'),
    'separate_emi': bi('Do you have any other monthly loan repayments that you would keep separate?',
                       'వీటి కాకుండా వేరుగా కొనసాగించే ఇతర EMIలు ఉన్నాయా అండి?'),
    'separate_emi_amount': bi('Approximately how much do those other monthly EMIs total?',
                              'ఆ ఇతర EMIలు నెలకు మొత్తం ఎంత అండి?'),
    'advisor': bi('Would you like an advisor to call and explain your options, EMI, interest rate and charges?',
                  'లోన్ ఆప్షన్లు, EMI, వడ్డీ, ఛార్జీల గురించి అడ్వైజర్ ఫోన్ చేసి వివరించాలా అండి?'),
    'consolidation_advisor': bi('An advisor can compare your current payments with a proposed single EMI and the total cost, including charges. Would you like a callback?',
                                'ఇప్పటి చెల్లింపులతో ఒక EMI ఆప్షన్, మొత్తం ఖర్చు, ఛార్జీలను మా అడ్వైజర్ పోల్చి వివరిస్తారు. కాల్ బ్యాక్ కావాలా అండి?'),
    'callback_time': bi('What day and time would suit you for a callback?',
                        'మీకు ఏ రోజు, ఏ టైమ్‌కి కాల్ బ్యాక్ కావాలి అండి?'),
    'callback_day': bi('Which day would suit you for a callback?',
                       'ఏ రోజు కాల్ బ్యాక్ కావాలి అండి?'),
    'callback_clock': bi('What time would suit you?', 'ఏ టైమ్‌కి కాల్ బ్యాక్ కావాలి అండి?'),
}

RESPONSES = {
    'retry': bi('Sorry, I didn’t catch that. {question}', 'క్షమించండి అండి, సరిగ్గా అర్థం కాలేదు. {question}'),
    'decline': bi('Understood. Thank you for your time.', 'సరే అండి, మీ సమయానికి ధన్యవాదాలు.'),
    'not_salaried': bi('Thanks for letting me know. This offer is for salaried employees. Thank you for your time.',
                       'చెప్పినందుకు ధన్యవాదాలు అండి. ఈ ఆఫర్ శాలరీ ఉద్యోగుల కోసం మాత్రమే. మీ సమయానికి ధన్యవాదాలు.'),
    'wrong_person': bi('Sorry for the disturbance. Thank you.', 'డిస్టర్బ్ చేసినందుకు క్షమించండి అండి. ధన్యవాదాలు.'),
    'opt_out': bi('Understood. I’ll end the call and record your request to stop promotional calls.',
                  'అర్థమైంది అండి. కాల్ ముగించి ప్రొమోషనల్ కాల్స్ ఆపమనే మీ రిక్వెస్ట్ నమోదు చేయడానికి ప్రయత్నిస్తాను.'),
    'opt_out_saved': bi('Your request to stop promotional calls has been recorded. Thank you.',
                        'ప్రొమోషనల్ కాల్స్ ఆపమనే మీ రిక్వెస్ట్ నమోదైంది అండి. ధన్యవాదాలు.'),
    'opt_out_failed': bi('I’ll end this call. I could not confirm your opt-out was saved; please contact our support team.',
                         'ఈ కాల్ ముగిస్తున్నాను అండి. రిక్వెస్ట్ నమోదైందని నిర్ధారించలేకపోయాను. దయచేసి సపోర్ట్ టీమ్‌ని సంప్రదించండి.'),
    'hangup': bi('Thank you for your time. Goodbye.', 'మీ సమయానికి ధన్యవాదాలు అండి. నమస్కారం.'),
    'busy': bi('Of course. When would be a better day and time to call?', 'సరే అండి. ఏ రోజు, ఏ టైమ్‌కి మళ్లీ కాల్ చేయవచ్చు?'),
    'purpose_clarify': bi('Sure. Is that for a new personal loan or to combine existing payments into one EMI?',
                          'సరే అండి. కొత్త పర్సనల్ లోన్ కావాలా, లేక ఉన్న EMIలను కలపాలనుకుంటున్నారా?'),
    'advisor_review': bi('An advisor can review your details and explain what options may be available.',
                         'మీ వివరాలను అడ్వైజర్ రివ్యూ చేసి అందుబాటులో ఉండే ఆప్షన్లు వివరిస్తారు.'),
    'estimate': bi('Based on what you shared, a preliminary illustrative loan estimate is around {amount}. The final amount and terms depend on HDFC Bank’s review.',
                   'మీ వివరాల ప్రకారం ప్రాథమిక అంచనా సుమారుగా {amount} అండి. తుది లోన్ మొత్తం, నిబంధనలు HDFC బ్యాంక్ పరిశీలనపై ఆధారపడి ఉంటాయి.'),
    'rate': bi('The interest rate and charges depend on the verified offer and your profile. An advisor can explain them.',
               'వడ్డీ రేటు, ఛార్జీలు మీ ప్రొఫైల్, ధృవీకరించిన ఆఫర్‌పై ఆధారపడి ఉంటాయి అండి. అడ్వైజర్ వివరిస్తారు.'),
    'guarantee': bi('This is only an initial estimate, not an approval. The bank makes the final decision.',
                    'ఇది కేవలం ప్రాథమిక అంచనా అండి, అప్రూవల్ కాదు. తుది నిర్ణయం బ్యాంక్‌దే.'),
    'who': bi('I’m Ramya, an AI assistant from {company_name}, helping customers explore HDFC Bank personal-loan options.',
              'నేను {company_name} నుంచి AI అసిస్టెంట్ రమ్యను. HDFC బ్యాంక్ పర్సనల్ లోన్ ఆప్షన్ల గురించి సమాచారం ఇస్తున్నాను.'),
    'audible': bi('Yes, I can hear you clearly.', 'అవును అండి, నాకు బాగా వినిపిస్తోంది.'),
    'security': bi('Please don’t share OTPs, passwords, card numbers or banking credentials on this call.',
                   'దయచేసి OTPలు, పాస్‌వర్డ్‌లు, కార్డ్ నంబర్లు చెప్పకండి అండి.'),
    'consolidation_disclaimer': bi('A single EMI may not lower your total cost. An advisor must check the rate, tenure and charges.',
                                  'ఒక EMIతో మొత్తం ఖర్చు తగ్గుతుందనే గ్యారంటీ లేదు అండి. వడ్డీ, కాలవ్యవధి, ఛార్జీలు పోల్చాలి.'),
    'callback_saved': bi('Thank you. I’ve recorded your callback request for {time}.',
                         'ధన్యవాదాలు అండి. {time} కి కాల్ బ్యాక్ రిక్వెస్ట్ నమోదు చేశాను.'),
    'callback_failed': bi('I couldn’t confirm that your callback request was saved. Please contact our support team.',
                          'మీ కాల్ బ్యాక్ రిక్వెస్ట్ సేవ్ అయిందని నిర్ధారించలేకపోయాను అండి. దయచేసి సపోర్ట్ టీమ్‌ని సంప్రదించండి.'),
    'language': bi('Certainly. {question}', 'తప్పకుండా అండి. {question}'),
}


def normalized(text: str) -> str:
    return ' '.join(re.sub(r'[^\w\s\u0C00-\u0C7F]', ' ', (text or '').lower().replace('’', '').replace("'", '')).split())


def yes_no(text: str) -> Optional[bool]:
    t = normalized(text)
    if not t:
        return None
    if any(x in t for x in ('not interested', 'dont want', 'do not want', 'no thank', 'not now', 'అవసరం లేదు', 'వద్దు')):
        return False
    if t in {'no', 'nope', 'no thanks', 'no thank you', 'not salaried', 'ledu', 'ledhu', 'కాదు', 'లేదు', 'కాదండి'}:
        return False
    if t in {'yes', 'yeah', 'yep', 'sure', 'ok', 'okay', 'correct', 'right', 'fine',
             'haan', 'ha', 'avunu', 'avunandi', 'aunu', 'sare', 'kavali', 'అవును', 'అవునండి', 'సరే', 'ఓకే', 'కావాలి'}:
        return True
    if t.startswith(('yes ', 'yeah ', 'sure ', 'of course ', 'అవును ', 'సరే ')):
        return True
    if t.startswith(('no ', 'లేదు ', 'కాదు ')):
        return False
    return None


def detect_purpose(text: str) -> Optional[str]:
    t = normalized(text)
    if any(x in t for x in (
        'combine', 'consolidat', 'single emi', 'one emi', 'merge', 'credit card balance',
        'existing loans', 'all my emis', 'debts', 'debt', 'outstanding balance',
        'combine my loans', 'ఒక emi', 'కలప', 'బకాయి', 'క్రెడిట్ కార్డ్')):
        return 'consolidate'
    if any(x in t for x in ('new personal loan', 'fresh loan', 'new loan', 'personal loan',
                            'need a loan', 'want a loan', 'borrow', 'loan kavali', 'కొత్త', 'పర్సనల్ లోన్')):
        return 'new'
    return None


def employment_value(text: str) -> Optional[bool]:
    t = normalized(text)
    negative = ('not salaried', 'no salary', 'no job', 'unemployed', 'self employed', 'self employed',
                'freelancer', 'not working', 'no work', 'i do business', 'own a shop', 'i run a business', 'i own a business', 'business owner', 'own business', 'student',
                'జాబ్ లేదు', 'బిజినెస్', 'ఉద్యోగం లేదు', 'శాలరీ లేదు', 'జాబ్ చేయడం లేదు')
    if any(x in t for x in negative):
        return False
    positive = ('salaried', 'employee', 'working in', 'working at', 'work at', 'work in',
                'i have a job', 'i am working', 'it job', 'government job', 'private job',
                'job chestunna', 'salary job', 'జాబ్ చేస్తున్న', 'ఉద్యోగం చేస్తున్న', 'శాలరీ ఉద్యోగం')
    if any(x in t for x in positive):
        return True
    return yes_no(text)


def parse_money(text: str) -> Optional[float]:
    t = (text or '').lower().replace(',', '')
    lpa_match = re.search(r'(?<![\w.])(\d+(?:\.\d+)?)\s*lpa\b', t, re.IGNORECASE)
    if lpa_match:
        return round(float(lpa_match.group(1)) * 100000, 2)
    dollar_match = re.search(
        r'(?:\$\s*|\b(?:usd|us dollars?|dollars?)\s*)(\d+(?:\.\d+)?)\b',
        t, re.IGNORECASE,
    )
    if dollar_match:
        return float(dollar_match.group(1))
    t = (text or '').lower().replace(',', '').replace('₹', ' ')
    units = {'crore': 1e7, 'crores': 1e7, 'cr': 1e7, 'కోటి': 1e7,
             'lakh': 1e5, 'lakhs': 1e5, 'lac': 1e5, 'lacs': 1e5, 'లక్ష': 1e5, 'లక్షలు': 1e5,
             'thousand': 1e3, 'k': 1e3, 'వేలు': 1e3}
    tokens = re.findall(r'(\d+(?:\.\d+)?)\s*(crores?|cr|lakhs?|lacs?|lac|thousand|k|కోటి|లక్షలు|లక్ష|వేలు)\b',t)
    if tokens:
        return round(sum(float(n) * units[u] for n, u in tokens), 2)

    # Spoken amounts are common in phone calls.  The old parser matched only
    # one word before a unit, so "ninety six thousand" could fall through to
    # the LLM extractor and be misread as 6,000.  Parse the complete phrase
    # locally so corrections always replace the stored value deterministically.
    number_words = {
        'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
        'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
        'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
        'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
        'nineteen': 19, 'twenty': 20, 'thirty': 30, 'forty': 40,
        'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80,
        'ninety': 90,
    }
    scales = {'hundred': 100, 'thousand': 1_000, 'lakh': 100_000,
              'lakhs': 100_000, 'lac': 100_000, 'lacs': 100_000,
              'crore': 10_000_000, 'crores': 10_000_000}
    spoken_tokens = re.findall(r'[a-z]+', t.replace('-', ' '))
    for start, token in enumerate(spoken_tokens):
        if token not in number_words:
            continue
        total = 0
        current = 0
        saw_scale = False
        consumed = False
        for word in spoken_tokens[start:]:
            if word == 'and':
                continue
            if word in number_words:
                current += number_words[word]
                consumed = True
                continue
            if word in scales:
                scale = scales[word]
                current = max(current, 1)
                if scale == 100:
                    current *= scale
                else:
                    total += current * scale
                    current = 0
                saw_scale = True
                continue
            break
        if consumed and saw_scale:
            return float(total + current)

    words = {'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,
             'nine':9,'ten':10,'twenty':20,'thirty':30,'forty':40,'fifty':50,
             'sixty':60,'seventy':70,'eighty':80,'ninety':90}
    for word, n in words.items():
        m = re.search(rf'\b{word}\s+(crores?|lakhs?|lacs?|thousand)\b',t)
        if m:
            return float(n * units[m.group(1)])
    numbers = re.findall(r'(?<![\w.])\d+(?:\.\d+)?(?![\w.])',t)
    if len(numbers)==1:
        return float(numbers[0])
    if re.search(r'\b(no|none|zero|nothing|nil)\b|లేవు|లేదు|సున్నా',t):
        return 0.0
    return None


def rupees(value: Any) -> str:
    if isinstance(value,(int,float)):
        if value >= 10000000 and value % 10000000 == 0:
            return f'₹{value/10000000:g} crore'
        if value >= 100000 and value % 100000 == 0:
            return f'₹{value/100000:g} lakh'
        return f'₹{value:,.0f}'
    return 'not supplied'


def money_display(value: Any, unit: Optional[str] = None) -> str:
    """Format an amount using the unit the customer explicitly supplied."""
    if unit == 'lpa' and isinstance(value, (int, float)):
        return f'{value / 100000:g} LPA'
    if unit == 'dollars' and isinstance(value, (int, float)):
        return f'{value:g} dollars'
    return rupees(value)


def money_unit(text: str) -> Optional[str]:
    """Return an explicitly spoken display unit, if one was provided."""
    t = (text or '').lower()
    if re.search(r'\blpa\b', t):
        return 'lpa'
    if re.search(r'(?:\$|\busd\b|\bus dollars?\b|\bdollars?\b)', t):
        return 'dollars'
    return None


def callback_parts(text: str) -> tuple[bool, bool]:
    t = normalized(text)

    # 1. Day patterns (English + Telugu + Roman Telugu + Dates)
    day_patterns = (
        r'\b(today|tomorrow|tonight|day after tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next week|weekend)\b',
        r'\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b',
        r'\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b',
        r'ఈరోజు|ఈ\s*రోజు|రేపు|ఎల్లుండి|సోమవారం|మంగళవారం|బుధవారం|గురువారం|శుక్రవారం|శనివారం|ఆదివారం|వచ్చే\s*వారం',
        r'\b(repu|eroju|ellundi|somavaram|mangalavaram|budhavaram|guruvaram|sukravaram|sanivaram|adivaram)\b',
    )
    has_day = bool(re.search('|'.join(day_patterns), t, re.IGNORECASE))

    # 2. Time patterns (English + Telugu + Roman Telugu + Digits + Periods of day)
    time_patterns = (
        # Explicit am/pm: e.g. 9 am, 9:30pm, 10a.m., 5 pm
        r'\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m|p\.m)\b',
        # Time with colon: e.g. 9:00, 10:30, 14:00
        r'\b(?:[0-1]?[0-9]|2[0-3]):[0-5][0-9]\b',
        # oclock / clock: 9 oclock, nine oclock, 9 o clock, 9oclock
        r'\b(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*(?:o\s*clock|oclock|clock)\b',
        # prepositions with numbers: at 9, around 10, by 5, after 4, between 2 and 4
        r'\b(?:at|around|by|after|before|between)\s+(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b',
        # periods of day (English + Roman Telugu)
        r'\b(morning|afternoon|evening|night|noon|midday|post lunch|lunch time|anytime|any time|udayam|madhyahnam|sayantram|ratri|ratiri)\b',
        # Telugu periods of day:
        r'ఉదయం|ఉదయమే|మార్నింగ్|మధ్యాహ్నం|ఆఫ్టర్నూన్|సాయంత్రం|ఈవినింగ్|రాత్రి|నైట్',
        # Telugu o'clock & time terms:
        r'గంటలకి|గంటలకు|గంటల|గంటలు|గంట|టైమ్|టైమ్‌కి|టైంకి|సమయానికి|సమయంలో',
        # Telugu hour inflections:
        r'ఒంటిగంటకి|రెండింటికి|మూడింటికి|నాలుగింటికి|ఐదింటికి|ఆరింటికి|ఏడింటికి|ఎనిమిదింటికి|తొమ్మిదింటికి|పదింటికి|పదకొండింటికి|పన్నెండింటికి',
        # Telugu number words with time:
        r'(?:ఒకటి|రెండు|మూడు|నాలుగు|ఐదు|ఆరు|ఏడు|ఎనిమిది|తొమ్మిది|పది|పదకొండు|పన్నెండు)\s*(?:గంట|టైమ్|గంటలకి|గంటలకు)?',
        # English number words (one to twelve):
        r'\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b',
    )
    has_time = bool(re.search('|'.join(time_patterns), t, re.IGNORECASE))

    # Standalone hour digits (1-12) or combined e.g. 'tomorrow 9', 'at 10'
    if not has_time and re.search(r'\b(?:[1-9]|1[0-2])\b', t):
        has_time = True

    return has_day, has_time


@dataclass
class CallFlow:
    customer_name: str = ''
    language: str = 'en'
    language_locked: bool = False
    answers: Dict[str,Any] = field(default_factory=dict)
    pending: str = 'purpose'
    status: str = 'in_progress'
    retry_count: int = 0
    outcome: str = ''
    last_customer_text: str = ''
    callback_requested: bool = False
    amount_units: Dict[str, str] = field(default_factory=dict)

    def snapshot(self)->Dict[str,Any]:
        return {'customer_name':self.customer_name,'language':self.language,
                'answers':dict(self.answers),'pending':self.pending,'status':self.status,
                'outcome':self.outcome,'callback_requested':self.callback_requested,
                'amount_units':dict(self.amount_units)}


@dataclass
class Turn:
    text: str
    close: bool=False
    persist: Optional[str]=None
    lead_status: Optional[str]=None


def say(code:str,s:CallFlow,**kwargs:Any)->str:
    return RESPONSES[code][s.language].format(company_name=company_name(),**kwargs)


# Short, deterministic answers for questions customers commonly ask while the
# scripted flow is waiting for an answer.  Keeping these out of the LLM path
# prevents invented loan terms and prevents the current question being echoed
# twice by the LLM plus the flow engine.
TELUGU_FAQS = (
    (('కొన్ని సాధారణ వివరాలతో', 'ఐదు నిమిషాల్లో ప్రాథమిక'),
     'అవును అండి. కొన్ని సాధారణ వివరాలు తీసుకుని సుమారు ఐదు నిమిషాల్లో ప్రారంభ అర్హతను పరిశీలించవచ్చు. ఇది తుది ఆమోదం కాదు.'),
    (('ఎందుకు కాల్', 'ఎందుకు ఫోన్'),
     'మీకు HDFC బ్యాంక్ పర్సనల్ లోన్ ఆప్షన్ల గురించి సమాచారం ఇవ్వడానికి ఈ కాల్ చేశాను.'),
    (('ఏ బ్యాంక్', 'బ్యాంక్ నుంచి', 'బ్యాంక్ నుండి'),
     'నేను {company_name} నుంచి AI అసిస్టెంట్ రమ్యను. HDFC బ్యాంక్ పర్సనల్ లోన్ ఆప్షన్ల గురించి సమాచారం ఇస్తున్నాను. నేను బ్యాంక్ ఉద్యోగినని చెప్పడం లేదు.'),
    (('మనిషా', 'మనిషి', 'ai ఆ', 'ఏఐ ఆ'),
     'నేను మనిషిని కాదు, AI అసిస్టెంట్‌ని. మీ ప్రాథమిక వివరాలు తీసుకుని సాధారణ సమాచారం ఇస్తాను. తుది నిర్ణయం బ్యాంక్ పరిశీలనపై ఆధారపడి ఉంటుంది.'),
    (('పర్సనల్ లోన్ అంటే', 'పర్సనల్ లోన్ ఏంటి'),
     'పర్సనల్ లోన్ అనేది సాధారణంగా వ్యక్తిగత అవసరాల కోసం తీసుకునే అన unsecured loan. వడ్డీ, కాలవ్యవధి, అర్హత మీ ప్రొఫైల్ మరియు బ్యాంక్ ఆమోదించిన ఆఫర్‌పై ఆధారపడి ఉంటాయి.'),
    (('ఎంత వరకు లోన్', 'ఎంత లోన్ వస్తుంది', 'ఎంత లోన్ ఇస్తారు'),
     'మీకు ఎంత లోన్ అర్హత ఉంటుందో జీతం, ఉద్యోగ స్థిరత్వం, ప్రస్తుత EMIలు, క్రెడిట్ ప్రొఫైల్ మరియు బ్యాంక్ పరిశీలన తర్వాతే తెలుస్తుంది. ఖచ్చితమైన మొత్తం గ్యారంటీగా చెప్పలేను.'),
    (('మినిమమ్ లోన్', 'కనీస లోన్'),
     'కనీస లోన్ మొత్తం ప్రస్తుత ఆఫర్ మరియు బ్యాంక్ నిబంధనలపై ఆధారపడి ఉంటుంది. సరైన మొత్తాన్ని అడ్వైజర్ ధృవీకరించాలి.'),
    (('పది లక్షల', '10 లక్షల'),
     'పది లక్షల లోన్ వస్తుందో లేదో మీ ఆదాయం, EMIలు, క్రెడిట్ ప్రొఫైల్ మరియు బ్యాంక్ అర్హత పరిశీలనపై ఆధారపడి ఉంటుంది. ముందుగా అప్రూవల్ గ్యారంటీ ఇవ్వలేను.'),
    (('ఇంట్రెస్ట్ రేట్', 'వడ్డీ రేట్', 'వడ్డీ ఎంత'),
     'ఖచ్చితమైన వడ్డీ రేటు మీ ప్రొఫైల్ మరియు ధృవీకరించిన ఆఫర్‌పై ఆధారపడి ఉంటుంది. అడ్వైజర్ రేటు, కాలవ్యవధి మరియు ఛార్జీలను స్పష్టంగా వివరిస్తారు.'),
    (('ఫిక్స్', 'మారుతుందా'),
     'రేటు ఫిక్స్‌డ్ లేదా మారే విధానం ఏ ఆఫర్ వర్తిస్తుందో దానిపై ఆధారపడి ఉంటుంది. మీకు వర్తించే నిబంధనలను అడ్వైజర్ ధృవీకరిస్తారు.'),
    (('ఎంత కాలం', 'కాలవ్యవధి'),
     'లోన్ కాలవ్యవధి ఆమోదించబడిన ఆఫర్‌పై ఆధారపడి ఉంటుంది. అందుబాటులో ఉన్న కాలవ్యవధులు మరియు మొత్తం చెల్లింపును అడ్వైజర్ చెప్తారు.'),
    (('ఐదు లక్షల లోన్', 'నెలకి emi', 'నెలకు emi'),
     'ఐదు లక్షల లోన్‌కి EMI వడ్డీ రేటు మరియు కాలవ్యవధి లేకుండా ఖచ్చితంగా చెప్పలేం. ఆ వివరాలు ధృవీకరించిన తర్వాత అడ్వైజర్ సరైన EMIని చెప్తారు.'),
    (('ప్రాసెస్ ఎలా', 'ప్రక్రియ ఎలా'),
     'ముందుగా ప్రాథమిక వివరాలు తీసుకుంటాం. తర్వాత అర్హత, డాక్యుమెంట్లు, రేటు మరియు ఛార్జీలను ధృవీకరించి, బ్యాంక్ పరిశీలనకు పంపుతారు. తుది ఆమోదం బ్యాంక్‌దే.'),
    (('ఎన్ని రోజులు', 'ఎప్పుడు అప్రూవల్', 'అప్రూవల్ రావడానికి'),
     'ఆమోద సమయం మీ డాక్యుమెంట్లు, ధృవీకరణ మరియు బ్యాంక్ ప్రాసెసింగ్‌పై ఆధారపడి ఉంటుంది. ఖచ్చితమైన సమయం గ్యారంటీగా చెప్పలేను.'),
    (('ప్రాసెసింగ్ ఫీజు', 'ఫీజు ఏమైనా'),
     'ప్రాసెసింగ్ ఫీజు మరియు ఇతర ఛార్జీలు ధృవీకరించిన ఆఫర్‌లో ఉంటే ముందుగా తెలియజేస్తారు. చెల్లించే ముందు అధికారిక వివరాలు నిర్ధారించుకోండి.'),
    (('నమ్మాలి', 'స్కామ్ కాల్', 'ప్రూఫ్ ఏంటి'),
     'మీరు OTP, పాస్‌వర్డ్, PIN లేదా బ్యాంకింగ్ వివరాలు ఈ కాల్‌లో చెప్పకండి. కంపెనీ అధికారిక నంబర్ లేదా అధికారిక ఛానల్ ద్వారా మమ్మల్ని స్వతంత్రంగా ధృవీకరించుకుని మాత్రమే ముందుకు వెళ్లండి.'),
    (('ఫోన్ నంబర్', 'నంబర్ ఎక్కడ'),
     'మీ నంబర్‌ను ఏ మూలం నుంచి పొందామో నేను ఈ కాల్‌లో ధృవీకరించలేను. మీకు కాల్స్ వద్దంటే చెప్పండి; మీ అభ్యర్థనను నమోదు చేయడానికి ప్రయత్నిస్తాను.'),
    (('గ్యారెంటీ', 'కచ్చితంగా అప్రూవ్'),
     'లోన్ ఆమోదానికి నేను గ్యారంటీ ఇవ్వలేను. తుది నిర్ణయం మీ వివరాలు మరియు డాక్యుమెంట్లను పరిశీలించిన తర్వాత బ్యాంక్‌దే.'),
    (('డాక్యుమెంట్స్ లేకుండా', 'డాక్యుమెంట్లు లేకుండా'),
     'అవసరమైన డాక్యుమెంట్లు బ్యాంక్ ధృవీకరణ కోసం కావచ్చు. ఏ డాక్యుమెంట్లు అవసరమో అధికారిక ఆఫర్ లేదా అడ్వైజర్ ద్వారా మాత్రమే నిర్ధారించుకోండి.'),
    (('ఈరోజే డబ్బులు', 'ఈ రోజు డబ్బులు'),
     'ఈరోజే డబ్బులు వస్తాయని హామీ ఇవ్వలేను. ఆమోదం, డాక్యుమెంట్ ధృవీకరణ మరియు బ్యాంక్ ప్రాసెసింగ్ పూర్తయిన తర్వాతే డిస్బర్స్‌మెంట్ జరుగుతుంది.'),
    (('emi కట్టకపోతే', 'EMI కట్టకపోతే'),
     'EMI ఆలస్యం అయితే ఛార్జీలు, క్రెడిట్ స్కోర్‌పై ప్రభావం లేదా ఇతర నిబంధనలు ఉండవచ్చు. మీ రుణ ఒప్పందాన్ని చూసి బ్యాంక్ అధికారిక సపోర్ట్‌ను సంప్రదించండి.'),
    (('ముందుగానే క్లోజ్', 'ప్రీక్లోజ్', 'క్లోజ్ చేయవచ్చా'),
     'లోన్‌ను ముందుగా మూసివేయడం సాధ్యమా, పెనాల్టీ ఉందా అనేది ఆఫర్ నిబంధనలపై ఆధారపడి ఉంటుంది. అడ్వైజర్ లేదా అధికారిక ఒప్పందం ద్వారా నిర్ధారించుకోండి.'),
    (('మెసేజ్ చేస్తారా', 'వివరాలు మెసేజ్'),
     'అధికారిక ఛానల్ అందుబాటులో ఉంటే వివరాలు పంపే విధానాన్ని అడ్వైజర్ చెప్తారు. OTP లేదా రహస్య బ్యాంకింగ్ వివరాలు ఎవరితోనూ పంచుకోకండి.'),
    (('మేనేజర్‌తో', 'మేనేజర్ తో'),
     'మీ అభ్యర్థనను అడ్వైజర్ లేదా సూపర్వైజర్‌కు పంపవచ్చు. మీకు అనుకూలమైన కాల్‌బ్యాక్ సమయం చెప్పండి.'),
    (('పర్సనల్ డీటెయిల్స్', 'వివరాలు ఏం చేస్తారు'),
     'మీ సమ్మతితో ఇచ్చిన వివరాలను ఈ అర్హత పరిశీలన మరియు ఫాలో-అప్ కోసం మాత్రమే ఉపయోగించాలి. OTP, పాస్‌వర్డ్, PIN లేదా పూర్తి కార్డ్ వివరాలు మాత్రం చెప్పకండి.'),
    (('it కంపెనీ', 'ప్రైవేట్ కంపెనీ'),
     'IT లేదా ప్రైవేట్ కంపెనీలో శాలరీ ఉద్యోగం ఉంటే అర్హత పరిశీలించవచ్చు. తుది నిర్ణయం జీతం, ఉద్యోగ స్థిరత్వం, EMIలు మరియు బ్యాంక్ పరిశీలనపై ఆధారపడి ఉంటుంది.'),
    (('నెల జీతం', 'నెలకి ఎనభై', 'జీతం ఇరవై', 'శాలరీ వస్తుంది'),
     'జీతం మొత్తం ఒక్కటే నిర్ణయాత్మక అంశం కాదు. ప్రస్తుత EMIలు, ఉద్యోగ స్థిరత్వం, క్రెడిట్ ప్రొఫైల్ మరియు బ్యాంక్ నిబంధనలతో కలిపి అర్హతను పరిశీలిస్తారు.'),
    (('కొత్తగా కంపెనీలో', 'కొత్తగా జాయిన్', 'జాయిన్ అయ్యాను'),
     'కొత్తగా ఉద్యోగంలో చేరినప్పుడు ఉద్యోగ స్థిరత్వం మరియు అవసరమైన ధృవీకరణను బ్యాంక్ పరిశీలించవచ్చు. తుది అర్హతను ముందుగా గ్యారంటీగా చెప్పలేను.'),
    (('రెండు లోన్లు', 'ఇప్పటికే లోన్లు', 'ఇంకొకటి వస్తుందా'),
     'ఇప్పటికే ఉన్న లోన్లు, వాటి EMIలు, ఆదాయం మరియు క్రెడిట్ ప్రొఫైల్ ఆధారంగా అదనపు లోన్ అర్హతను పరిశీలిస్తారు. తుది నిర్ణయం బ్యాంక్‌దే.'),
    (('క్యాష్ రూపంలో', 'క్యాష్ జీతం'),
     'క్యాష్ జీతం ఉన్నప్పుడు ఆదాయాన్ని ధృవీకరించడానికి అదనపు పత్రాలు అవసరం కావచ్చు. అర్హత మరియు డాక్యుమెంట్లను అడ్వైజర్ నిర్ధారిస్తారు.'),
    (('ఉద్యోగం లేదు', 'జాబ్ లేదు', 'కిరాణా షాప్', 'బిజినెస్', 'ఫ్రీలాన్సర్', 'రిటైర్'),
     'ఈ ప్రారంభ ఆఫర్ ప్రస్తుతం శాలరీ ఉద్యోగుల కోసం మాత్రమే. మీ వివరాలను తీసుకుని తగిన తదుపరి సమాచారం అడ్వైజర్ ఇవ్వగలరు.'),
    (('సిబిల్', 'క్రెడిట్ స్కోర్'),
     'క్రెడిట్ స్కోర్ అర్హతలో ఒక అంశం మాత్రమే. స్కోర్ తెలియకపోతే అధికారిక క్రెడిట్ బ్యూరో లేదా బ్యాంక్ అధికారిక ఛానల్ ద్వారా మాత్రమే తెలుసుకోండి; ఖచ్చితమైన ఆమోదం గ్యారంటీ కాదు.'),
)


def faq_answer(text: str, s: CallFlow) -> Optional[str]:
    """Return a safe answer for a common Telugu FAQ, without adding a question."""
    if s.language != 'te':
        return None
    t = normalized(text)
    for phrases, answer in TELUGU_FAQS:
        if any(phrase in t for phrase in phrases):
            return answer.format(company_name=company_name())
    return None


def answer_with_question(answer: str, current_question: str) -> str:
    """Append the pending flow question only when the answer did not already include it."""
    if normalized(current_question) in normalized(answer):
        return answer.strip()
    return f'{answer.strip()} {current_question.strip()}'.strip()


def answer_customer_question(answer: str, customer_text: str, current_question: str) -> str:
    """Do not ask again when the customer has already asked the pending question."""
    if normalized(current_question) in normalized(customer_text):
        return answer.strip()
    return answer_with_question(answer, current_question)


def summary(s:CallFlow)->str:
    a=s.answers
    display = lambda key: money_display(a.get(key), s.amount_units.get(key))
    if s.language=='te':
        return f"మీకు {display('requested_amount')} లోన్ కావాలి, నెల శాలరీ {display('salary')}, ప్రస్తుత EMIలు, కార్డ్ పేమెంట్లు {display('existing_emi')}."
    return (f"You want {display('requested_amount')}; your monthly credited salary is "
            f"{display('salary')}; and your existing monthly loan and card payments total {display('existing_emi')}.")


def question(s:CallFlow, stage:Optional[str]=None)->str:
    key=stage or s.pending
    if key=='confirm':
        return QUESTIONS[key][s.language].format(summary=summary(s))
    return QUESTIONS[key][s.language].format(company_name=company_name())


def opening(s:CallFlow)->str:
    return question(s,'opening')


def estimate(s:CallFlow)->Optional[float]:
    """Opt-in illustrative affordability math; NEVER an HDFC eligibility API."""
    if os.getenv('ENABLE_PERSONAL_LOAN_ESTIMATE','0')!='1':
        return None
    try:
        rate=float(os.environ['PERSONAL_LOAN_INTEREST_RATE'])
        foir=float(os.environ['PERSONAL_LOAN_FOIR_RATIO'])
        months=int(os.environ['PERSONAL_LOAN_TENURE_MONTHS'])
        income=float(s.answers['salary']); debts=float(s.answers['existing_emi'])
        requested=float(s.answers['requested_amount'])
        if not all(map(math.isfinite,(rate,foir,income,debts,requested))) or not (0<rate<40 and 0<foir<0.9 and 1<=months<=84 and income>0 and requested>0 and debts>=0):
            return None
        max_emi=income*foir-debts
        if max_emi<=0:
            return None
        monthly_rate=rate/1200
        max_principal=max_emi*(1-(1+monthly_rate)**(-months))/monthly_rate
        amount=min(max_principal,requested)
        return round(amount,-2) if math.isfinite(amount) and amount>=1000 else None
    except (KeyError,ValueError,TypeError,ArithmeticError):
        return None



MONEY_CUES = {
    'requested_amount': ('loan amount', 'want a loan', 'need a loan', 'borrow', 'loan of', 'loan for', 'లోన్ కావాలి'),
    'salary': ('salary', 'take home', 'credited', 'earn monthly', 'monthly income', 'శాలరీ', 'జీతం'),
    'existing_emi': ('emi', 'repayment', 'card payment', 'pay each month', 'ఈఎంఐ'),
}


def _amount_for_stage(stage: str, text: str) -> Optional[float]:
    """Avoid treating two clearly different amounts as one; retain compound lakh amounts."""
    chunks = [x.strip() for x in re.split(r'[,;]|\s+(?:and|plus|మరియు)\s+', text, flags=re.IGNORECASE) if x.strip()]
    if len(chunks) <= 1:
        return parse_money(text)
    cues = MONEY_CUES.get(stage, ())
    targeted = [c for c in chunks if any(cue in normalized(c) for cue in cues)]
    if len(targeted) == 1:
        return parse_money(targeted[0])
    amounts = [parse_money(c) for c in chunks]
    nonempty = [x for x in amounts if x is not None]
    return nonempty[0] if len(nonempty) == 1 else None


def explicit_corrections(text: str) -> Dict[str, float]:
    """Extract all explicitly labelled money corrections from one sentence."""
    corrections: Dict[str, float] = {}
    chunks = [chunk.strip() for chunk in re.split(
        r'\b(?:and|but|also)\b|[,.;]', text, flags=re.IGNORECASE
    ) if chunk.strip()]
    cues = {
        'salary': ('salary', 'income', 'credited', 'జీతం', 'శాలరీ'),
        'requested_amount': ('loan amount', 'want a loan', 'need a loan',
                             'need ', 'borrow', 'లోన్ అమౌంట్', 'లోన్ మొత్తం'),
        'existing_emi': ('emi', 'repayment', 'credit card', 'monthly payment',
                         'కార్డ్', 'ఈఎంఐ'),
    }
    for chunk in chunks:
        low = normalized(chunk)
        value = parse_money(chunk)
        if value is None:
            continue
        for field, field_cues in cues.items():
            if any(cue in low for cue in field_cues):
                if value > 0 or field == 'existing_emi':
                    corrections[field] = value
                break
    return corrections


def capture_extra_details(s: CallFlow, stage: str, text: str) -> None:
    """Save only explicitly labeled other amounts; never guess from unrelated numbers."""
    if stage not in {'purpose','permission','employment','requested_amount','salary','existing_emi'}:
        return
    chunks = [x.strip() for x in re.split(r'[,;]|\s+(?:and|plus|మరియు)\s+', text, flags=re.IGNORECASE) if x.strip()]
    for chunk in chunks:
        low = normalized(chunk)
        for key, cues in MONEY_CUES.items():
            if key == stage or key in s.answers or not any(cue in low for cue in cues):
                continue
            money = parse_money(chunk)
            if money is not None and (money > 0 or key == 'existing_emi'):
                s.answers[key] = money

def stages(s:CallFlow)->list[str]:
    if s.answers.get('purpose')=='consolidate':
        fields=['purpose','permission','employment','salary','debts_to_combine',
                'consolidation_outstanding','consolidation_payment','separate_emi']
        if s.answers.get('separate_emi') is True:
            fields.append('separate_emi_amount')
        return fields+['consolidation_advisor','callback_time']
    return ['purpose','permission','employment','requested_amount','salary','existing_emi',
            'confirm','advisor','callback_time']


def next_stage(s:CallFlow)->str:
    for st in stages(s):
        if st=='callback_time' and not s.callback_requested:
            continue
        if st not in s.answers:
            return st
    return 'complete'


def extract_value(stage:str,text:str)->Any:
    if stage=='purpose':
        return detect_purpose(text)
    if stage=='employment':
        return employment_value(text)
    if stage in {'permission','advisor','consolidation_advisor','confirm','separate_emi'}:
        return yes_no(text)
    if stage in {'requested_amount','salary','existing_emi','consolidation_outstanding','consolidation_payment','separate_emi_amount'}:
        return _amount_for_stage(stage, text)
    if stage=='debts_to_combine':
        t=normalized(text)
        if 1<=len(t.split())<=35 and t not in ('no','yes','okay','dont know') and '?' not in text:
            return text.strip()[:250]
    return None


def is_opt_out(text:str)->bool:
    t=normalized(text)
    return any(x in t for x in ('stop calling','dont call','do not call','remove my number','no more calls','unsubscribe',
                              'stop promotional','కాల్ చేయకండి','మళ్లీ కాల్ వద్దు','కాల్ వద్దు','call cheyakandi'))


def is_decline(text:str)->bool:
    t=normalized(text)
    return any(x in t for x in ('not interested','i dont want a loan','do not want a loan',
                              'dont need any loan','no personal loan','loan vaddu','లోన్ వద్దు','లోన్ అవసరం లేదు'))


def is_busy(text:str)->bool:
    t=normalized(text)
    return any(x in t for x in ('im busy','i am busy','busy now','call later','call back later',
                              'call me later','at work now','బిజీగా','తర్వాత కాల్'))


def is_wrong_person(text:str)->bool:
    t=normalized(text)
    return any(x in t for x in ('wrong number','wrong person','not that person','తప్పు నంబర్','రాంగ్ నంబర్'))


def wants_end(text:str)->bool:
    return normalized(text) in {'bye','goodbye','hang up','cut the call','end the call','ఫోన్ పెట్టేయండి','కాల్ కట్ చేయండి'}


def special_response(text:str)->Optional[str]:
    t=normalized(text)
    if any(x in t for x in ('can you hear','are you there','am i audible','వినిపిస్తుందా')):
        return 'audible'
    if any(x in t for x in ('who is this','who are you','which company','which bank','are you from the bank','who speaking','మీరు ఎవరు','ఎవరు')):
        return 'who'
    if any(x in t for x in ('otp','password','cvv','pin number','account number')):
        return 'security'
    if any(x in t for x in ('guarantee','guaranteed approval','guaranteed loan','confirmed approval','గ్యారంటీ')):
        return 'guarantee'
    if any(x in t for x in ('interest rate','how much interest','how much rate','what rate','processing fee','charges','వడ్డీ ఎంత','ఇంట్రెస్ట్ ఎంత')):
        return 'rate'
    if any(x in t for x in ('guaranteed saving','lower emi guaranteed','save money guaranteed')):
        return 'consolidation_disclaimer'
    return None


def is_question(text:str)->bool:
    t=normalized(text)
    return '?' in text or t.startswith(('what ','why ','how ','who ','when ','which ','can you ',
                                        'could you ','tell me ','is there ','do you ','ఎంత','ఏంటి','ఎలా','చెప్పండి','entha ','enti '))


async def _llm_extract(s:CallFlow,stage:str,text:str)->Any:
    if os.getenv('ENABLE_LLM_FALLBACK','1')!='1':
        return None
    try:
        from components.openai_service import generate_chat_reply
        answer=await generate_chat_reply([
            {'role':'system','content':EXTRACTION_PROMPT},
            {'role':'user','content':f'Stage: {stage}\nQuestion: {question(s,stage)}\nCustomer said: {text}'}
        ], max_tokens=96)
        match=re.search(r'\{.*\}',answer,re.DOTALL)
        if not match:
            return None
        data=json.loads(match.group())
        if data.get('confidence')!='high':
            return None
        value=data.get('value')
        if value is None:
            return None
        if stage=='purpose':
            return value if value in {'new','consolidate'} else None
        if stage in {'permission','employment','confirm','advisor','consolidation_advisor','separate_emi'}:
            return {'yes':True,'no':False}.get(str(value).lower())
        return extract_value(stage,str(value))
    except Exception:
        return None


async def _llm_answer_question(s:CallFlow,text:str)->Optional[str]:
    if os.getenv('ENABLE_LLM_FALLBACK','1')!='1':
        return None
    try:
        from components.openai_service import generate_chat_reply
        lang=ENGLISH_LANGUAGE_INSTRUCTION if s.language=='en' else TELUGU_LANGUAGE_INSTRUCTION
        answer=await generate_chat_reply([
            {'role':'system','content':SYSTEM_PROMPT+'\n'+FALLBACK_QUESTION_PROMPT+'\n'+lang},
            {'role':'user','content':f'Company: {company_name()}\nCurrent question: {question(s)}\nCustomer: {text}'},
        ],max_tokens=80)
        answer=answer.strip()
        return answer if answer and not re.search(r'\b(otp|password|guaranteed approval)\b',answer.lower()) else None
    except Exception:
        return None


def _end(s:CallFlow,code:str,outcome:str,lead_status:str='not_interested')->Turn:
    s.status='ended'; s.outcome=outcome
    return Turn(say(code,s),close=True,lead_status=lead_status)


def _continue(s:CallFlow)->Turn:
    nxt=next_stage(s)
    if nxt=='complete':
        return _end(s,'hangup','completed','interested')
    s.pending=nxt
    return Turn(question(s,nxt))


async def respond(s:CallFlow, customer_text:str,detected_language:Optional[str]=None)->Turn:
    text=(customer_text or '').strip()
    if s.status=='ended':
        return Turn('')
    if not text:
        return Turn(say('retry',s,question=question(s)))
    if is_recording_announcement(text):
        return Turn(question(s))
    s.last_customer_text=text

    # Privacy, wrong-party and termination signals precede field capture.
    if is_opt_out(text):
        s.status='ended'; s.outcome='do_not_call'
        return Turn(say('opt_out',s),close=True,persist='opt_out',lead_status='not_interested')
    if is_wrong_person(text):
        return _end(s,'wrong_person','wrong_person')
    if is_decline(text):
        return _end(s,'decline','declined')
    if wants_end(text):
        return _end(s,'hangup','customer_ended')

    switch=detect_explicit_language_switch(text)
    if not s.language_locked:
        s.language=switch or detect_initial_call_language(text,detected_language)
        s.language_locked=True
    elif switch:
        s.language=switch
        return Turn(say('language',s,question=question(s)))

    if is_busy(text):
        s.callback_requested=True
        s.pending='callback_time'
        return Turn(say('busy',s))

    special=special_response(text)
    faq=faq_answer(text,s)
    if faq:
        return Turn(answer_customer_question(faq, text, question(s)))
    if special:
        return Turn(answer_customer_question(say(special,s), text, question(s)))

    stage=s.pending
    if normalized(text) in {'hello','hi','hey','హలో'}:
        return Turn(question(s))

    if stage=='correction':
        corrections = explicit_corrections(text)
        if corrections:
            s.answers.update(corrections)
            for key in corrections:
                unit = money_unit(text)
                if unit:
                    s.amount_units[key] = unit
            s.pending='confirm'
            return Turn(question(s,'confirm'))
        t=normalized(text)
        if any(x in t for x in ('salary','income','జీతం','శాలరీ')):
            key='salary'
        elif any(x in t for x in ('emi','repayment','credit card','కార్డ్','ఈఎంఐ')):
            key='existing_emi'
        elif any(x in t for x in ('loan amount','borrow','amount','లోన్ అమౌంట్','లోన్ మొత్తం')):
            key='requested_amount'
        else:
            return Turn(question(s,'correction'))
        value=parse_money(text)
        if value is not None and (value>0 or key=='existing_emi'):
            s.answers[key]=value
            s.pending='confirm'
            return Turn(question(s,'confirm'))
        s.answers.pop(key,None)
        s.answers.pop('confirm',None)
        s.pending=key
        return Turn(question(s,key))

    if stage=='callback_time':
        has_day,has_time=callback_parts(text)
        if has_day and has_time:
            preferred=text
        elif has_time and s.answers.get('_callback_day'):
            preferred=s.answers['_callback_day']+' '+text
        elif has_day and s.answers.get('_callback_clock'):
            preferred=text+' '+s.answers['_callback_clock']
        elif has_day:
            s.answers['_callback_day']=text
            return Turn(question(s,'callback_clock'))
        elif has_time:
            s.answers['_callback_clock']=text
            return Turn(question(s,'callback_day'))
        elif s.answers.get('_callback_day'):
            preferred=s.answers['_callback_day']+' '+text
        else:
            s.retry_count+=1
            return Turn(say('retry',s,question=question(s)))
        s.answers['callback_time']=preferred[:120]
        s.callback_requested=True
        s.status='ended';s.outcome='callback_requested'
        return Turn('',close=True,persist='callback',lead_status='follow_up')

    # Handle corrections before the generic confirm/LLM extraction. This lets
    # one sentence update multiple fields, for example: "Both are not correct;
    # salary is twenty thousand and I need one lakh loan."
    if stage == 'confirm' and yes_no(text) is not True:
        corrections = explicit_corrections(text)
        if corrections:
            s.answers.update(corrections)
            for key in corrections:
                unit = money_unit(text)
                if unit:
                    s.amount_units[key] = unit
            s.pending = 'confirm'
            return Turn(question(s, 'confirm'))

    value=extract_value(stage,text)
    if value is None and (len(text.split())>1 or any(c.isdigit() for c in text)) and not is_question(text):
        value=await _llm_extract(s,stage,text)

    if stage=='purpose':
        if value is None:
            if yes_no(text) is False:
                return _end(s,'decline','declined')
            if yes_no(text) is True:
                return Turn(say('purpose_clarify',s))
    elif stage=='permission' and value is False:
        return _end(s,'decline','declined')
    elif stage=='employment' and value is False:
        return _end(s,'not_salaried','not_salaried')
    elif stage in {'advisor','consolidation_advisor'} and value is False:
        return _end(s,'decline','declined_callback','interested')
    elif stage=='confirm' and value is False:
        # Apply an explicit correction immediately when the customer includes
        # both the field and the new amount in the same sentence, e.g.
        # "No, my monthly salary is ninety six thousand."
        corrections = explicit_corrections(text)
        if corrections:
            s.answers.update(corrections)
            for key in corrections:
                unit = money_unit(text)
                if unit:
                    s.amount_units[key] = unit
            s.pending = 'confirm'
            return Turn(question(s, 'confirm'))
        s.answers.pop('confirm',None)
        s.pending='correction'
        return Turn(question(s,'correction'))

    if value is None:
        if is_question(text):
            answer=await _llm_answer_question(s,text)
            if answer:
                return Turn(answer_with_question(answer, question(s)))
        s.retry_count+=1
        return Turn(say('retry',s,question=question(s)))

    if stage in {'requested_amount','salary','consolidation_outstanding','consolidation_payment'} and not (isinstance(value,(int,float)) and value>0):
        return Turn(say('retry',s,question=question(s)))
    if stage in {'existing_emi','separate_emi_amount'} and not (isinstance(value,(int,float)) and value>=0):
        return Turn(say('retry',s,question=question(s)))
    if stage=='debts_to_combine' and normalized(text) in {'nothing','none'}:
        return Turn(say('retry',s,question=question(s)))

    s.retry_count=0
    s.answers[stage]=value
    unit = money_unit(text)
    if unit:
        s.amount_units[stage] = unit
    capture_extra_details(s,stage,text)
    if stage=='existing_emi':
        s.pending='confirm'
        return Turn(question(s,'confirm'))
    if stage=='confirm':
        s.pending='advisor'
        amount=estimate(s)
        intro=say('estimate',s,amount=rupees(amount)) if amount is not None else say('advisor_review',s)
        return Turn(intro+' '+question(s,'advisor'))
    if stage=='separate_emi' and value is False:
        s.pending='consolidation_advisor'
        return Turn(question(s,'consolidation_advisor'))
    if stage=='separate_emi' and value is True:
        s.pending='separate_emi_amount'
        return Turn(question(s,'separate_emi_amount'))
    if stage=='separate_emi_amount':
        s.pending='consolidation_advisor'
        return Turn(question(s,'consolidation_advisor'))
    if stage in {'advisor','consolidation_advisor'}:
        s.callback_requested=True
        s.pending='callback_time'
        return Turn(question(s,'callback_time'))
    return _continue(s)


def confirmation_after_save(s:CallFlow,what:str,saved:bool)->str:
    if what=='callback':
        return say('callback_saved',s,time=s.answers.get('callback_time','')) if saved else say('callback_failed',s)
    if what=='opt_out':
        return say('opt_out_saved',s) if saved else say('opt_out_failed',s)
    return ''
