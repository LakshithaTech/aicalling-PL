"""Offline personal-loan flow tests. No real bank, telephony, credentials or LLM."""
import os
import unittest
from unittest.mock import patch
from components.personal_loan_flow import (
    CallFlow, opening, question, respond, estimate, parse_money, confirmation_after_save,
    summary,
    answer_with_question,
)


class PersonalLoanFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.p = patch.dict(os.environ, {
            'ENABLE_LLM_FALLBACK':'0','ENABLE_PERSONAL_LOAN_ESTIMATE':'0',
            'AGENT_COMPANY_NAME':'Demo Finance Partner',
        })
        self.p.start()

    def tearDown(self):
        self.p.stop()

    async def step(self, state, utterance):
        return await respond(state, utterance)

    async def test_opening_matches_script(self):
        s=CallFlow()
        o=opening(s)
        self.assertIn('Ramya',o)
        self.assertIn('calling you on behalf of HDFC Bank regarding personal loans',o)
        self.assertIn('one EMI',o)
        self.assertEqual(s.pending,'purpose')

    async def test_telugu_faq_answers_are_not_duplicated(self):
        cases = (
            ('మీరు ఏ బ్యాంక్ నుంచి కాల్ చేస్తున్నారు?', 'నేను Demo Finance Partner నుంచి AI అసిస్టెంట్ రమ్యను.'),
            ('మీరు నిజంగా మనిషా లేక AI ఆ?', 'నేను మనిషిని కాదు, AI అసిస్టెంట్‌ని.'),
            ('పర్సనల్ లోన్ అంటే ఏంటండి?', 'పర్సనల్ లోన్ అనేది'),
            ('నాకు ఎంత వరకు లోన్ వస్తుంది?', 'ఖచ్చితమైన మొత్తం గ్యారంటీగా చెప్పలేను.'),
        )
        for utterance, expected in cases:
            s = CallFlow()
            turn = await self.step(s, utterance)
            self.assertEqual(s.language, 'te')
            self.assertIn(expected, turn.text)
            self.assertEqual(turn.text.count('మీకు కొత్త పర్సనల్ లోన్ కావాలా'), 1)

    async def test_question_is_appended_only_once(self):
        question_text = 'మీరు ప్రస్తుతం శాలరీ వచ్చే ఉద్యోగం చేస్తున్నారా అండి?'
        answer = 'ఇంట్రెస్ట్ రేట్ గురించి ఖచ్చితమైన సమాచారం ఇవ్వలేను. ' + question_text
        self.assertEqual(answer_with_question(answer, question_text), answer)

    async def test_new_loan_flow_and_callback_persistence(self):
        s=CallFlow()
        for inp, nxt in (
            ('I need a new personal loan','permission'),
            ('Yes','employment'),
            ('I work in IT','requested_amount'),
            ('5 lakh','salary'),
            ('60000','existing_emi'),
            ('12000','confirm'),
            ('Yes','advisor'),
            ('Yes please','callback_time'),
        ):
            t=await self.step(s,inp)
            self.assertEqual(s.pending,nxt,repr((inp,t.text,s.answers)))
            self.assertLessEqual(t.text.count('?'),1)
        self.assertEqual(s.answers['requested_amount'],500000)
        self.assertEqual(s.answers['salary'],60000)
        self.assertEqual(s.answers['existing_emi'],12000)
        self.assertEqual(s.pending,'callback_time')
        t=await self.step(s,'tomorrow at 5 PM')
        self.assertTrue(t.close)
        self.assertEqual(t.persist,'callback')
        self.assertEqual(s.outcome,'callback_requested')
        self.assertIn('tomorrow at 5 PM',confirmation_after_save(s,'callback',True))
        self.assertNotIn('recorded your callback',confirmation_after_save(s,'callback',False))

    async def test_multiple_clear_values_in_one_customer_reply(self):
        s=CallFlow(answers={'purpose':'new','permission':True,'employment':True}, pending='requested_amount')
        t=await self.step(s,'I need a loan of 5 lakh, salary is 60000 and my EMI is 12000')
        self.assertEqual(s.answers['requested_amount'],500000)
        self.assertEqual(s.answers['salary'],60000)
        self.assertEqual(s.answers['existing_emi'],12000)
        self.assertEqual(s.pending,'confirm')

    async def test_no_salary_ends(self):
        s=CallFlow()
        for answer in ('new personal loan','yes'):
            await self.step(s,answer)
        t=await self.step(s,'I own a business')
        self.assertTrue(t.close)
        self.assertEqual(s.outcome,'not_salaried')
        self.assertNotIn('requested_amount',s.answers)

    async def test_consolidation_no_other_emi(self):
        s=CallFlow()
        for answer, nxt in (
            ('I want to combine all my existing loans','permission'),
            ('yes','employment'),
            ('yes','salary'),
            ('75000','debts_to_combine'),
            ('credit card and car loan','consolidation_outstanding'),
            ('4 lakh','consolidation_payment'),
            ('16000','separate_emi'),
            ('no','consolidation_advisor'),
            ('yes','callback_time'),
        ):
            turn=await self.step(s,answer)
            self.assertEqual(s.pending,nxt,(answer,turn.text,s.answers))
        t=await self.step(s,'Friday at 11 AM')
        self.assertEqual(t.persist,'callback')
        self.assertEqual(s.answers['separate_emi'],False)
        self.assertIn('credit card',s.answers['debts_to_combine'])

    async def test_consolidation_with_other_emi(self):
        s=CallFlow(answers={'purpose':'consolidate','permission':True,'employment':True,
                            'salary':70000,'debts_to_combine':'credit card',
                            'consolidation_outstanding':80000,'consolidation_payment':4000},pending='separate_emi')
        t=await self.step(s,'Yes')
        self.assertEqual(s.pending,'separate_emi_amount')
        self.assertLessEqual(t.text.count('?'),1)
        t=await self.step(s,'5000')
        self.assertEqual(s.pending,'consolidation_advisor')
        self.assertEqual(s.answers['separate_emi_amount'],5000)

    async def test_callback_day_then_time(self):
        s=CallFlow(pending='callback_time',callback_requested=True)
        t=await self.step(s,'tomorrow')
        self.assertFalse(t.close)
        self.assertIn('time',t.text.lower())
        t=await self.step(s,'4 PM')
        self.assertEqual(s.answers['callback_time'],'tomorrow 4 PM')
        self.assertEqual(t.persist,'callback')

    async def test_callback_time_then_day(self):
        s=CallFlow(pending='callback_time',callback_requested=True)
        t=await self.step(s,'4 PM')
        self.assertIn('day',t.text.lower())
        t=await self.step(s,'tomorrow')
        self.assertEqual(s.answers['callback_time'],'tomorrow 4 PM')
        self.assertEqual(t.persist,'callback')

    async def test_busy_callback(self):
        s=CallFlow()
        t=await self.step(s,'I am busy, call later')
        self.assertEqual(s.pending,'callback_time')
        self.assertFalse(t.close)
        t=await self.step(s,'next Wednesday at 10 AM')
        self.assertEqual(t.persist,'callback')

    async def test_opt_out(self):
        s=CallFlow()
        t=await self.step(s,"Don't call this number again")
        self.assertTrue(t.close)
        self.assertEqual(t.persist,'opt_out')
        self.assertEqual(s.outcome,'do_not_call')
        self.assertIn('recorded',confirmation_after_save(s,'opt_out',True))
        self.assertNotIn('has been recorded',confirmation_after_save(s,'opt_out',False))

    async def test_telugu_lock_and_explicit_switch(self):
        s=CallFlow()
        await self.step(s,'కొత్త పర్సనల్ లోన్ కావాలి')
        self.assertEqual(s.language,'te')
        await self.step(s,'yes')
        self.assertEqual(s.language,'te')
        t=await self.step(s,'Please speak in English')
        self.assertEqual(s.language,'en')
        self.assertIn('salaried employee',t.text)

    async def test_telugu_switch_after_english_call(self):
        s=CallFlow()
        await self.step(s,'I want a personal loan')
        self.assertEqual(s.language,'en')

        t=await self.step(s,'తెలుగులో మాట్లాడదామా?')
        self.assertEqual(s.language,'te')
        self.assertNotIn('English only',t.text)
        self.assertIn('కొన్ని సాధారణ వివరాలతో',t.text)

        t=await self.step(s,'I want to continue with you in Telugu')
        self.assertEqual(s.language,'te')
        self.assertNotIn('English only',t.text)

    async def test_disinterest(self):
        s=CallFlow()
        t=await self.step(s,'I am not interested')
        self.assertTrue(t.close)
        self.assertEqual(s.outcome,'declined')

    async def test_correction(self):
        s=CallFlow(answers={'purpose':'new','permission':True,'employment':True,
                            'requested_amount':300000,'salary':65000,'existing_emi':10000},pending='confirm')
        t=await self.step(s,'no')
        self.assertEqual(s.pending,'correction')
        t=await self.step(s,'salary is 75000')
        self.assertEqual(s.pending,'confirm')
        self.assertEqual(s.answers['salary'],75000)
        self.assertIn('₹75,000',t.text)

    async def test_calculator_off_and_enabled(self):
        s=CallFlow(answers={'purpose':'new','permission':True,'employment':True,
                            'requested_amount':500000,'salary':60000,'existing_emi':10000})
        self.assertIsNone(estimate(s))
        with patch.dict(os.environ,{'ENABLE_PERSONAL_LOAN_ESTIMATE':'1',
                                   'PERSONAL_LOAN_INTEREST_RATE':'12',
                                   'PERSONAL_LOAN_FOIR_RATIO':'0.5',
                                   'PERSONAL_LOAN_TENURE_MONTHS':'36'}):
            amount=estimate(s)
            self.assertGreater(amount,0)
            self.assertLessEqual(amount,500000)
        with patch.dict(os.environ,{'ENABLE_PERSONAL_LOAN_ESTIMATE':'1',
                                   'PERSONAL_LOAN_INTEREST_RATE':'bad',
                                   'PERSONAL_LOAN_FOIR_RATIO':'0.5',
                                   'PERSONAL_LOAN_TENURE_MONTHS':'36'}):
            self.assertIsNone(estimate(s))

    def test_money(self):
        self.assertEqual(parse_money('2 lakh'),200000)
        self.assertEqual(parse_money('none'),0)
        self.assertEqual(parse_money('12,000'),12000)
        self.assertEqual(parse_money('ninety thousand rupees'),90000)
        self.assertEqual(parse_money('ninety six thousand rupees'),96000)

    def test_lpa_and_dollar_units_are_preserved_for_summary(self):
        self.assertEqual(parse_money('4 LPA'),400000)
        self.assertEqual(parse_money('$4,000'),4000)
        s=CallFlow(answers={'requested_amount':500000,'salary':400000,'existing_emi':4000})
        s.amount_units={'salary':'lpa','existing_emi':'dollars'}
        text=summary(s)
        self.assertIn('4 LPA',text)
        self.assertIn('4000 dollars',text)
        self.assertNotIn('₹4 lakh',text)

    async def test_spoken_salary_correction_updates_final_summary(self):
        s=CallFlow(answers={'purpose':'new','permission':True,'employment':True,
                            'requested_amount':2000000,'salary':6000,'existing_emi':40000},
                   pending='salary')
        t=await self.step(s,'Ninety six thousand rupees')
        self.assertEqual(s.answers['salary'],96000)
        self.assertEqual(s.pending,'confirm')
        self.assertIn('₹96,000',t.text)
        self.assertNotIn('₹6,000',t.text)

    async def test_inline_salary_correction_updates_immediately(self):
        s=CallFlow(answers={'purpose':'new','permission':True,'employment':True,
                            'requested_amount':2000000,'salary':90000,'existing_emi':40000},
                   pending='confirm')
        t=await self.step(s,"No, it's not correct. My monthly salary is ninety six thousand.")
        self.assertEqual(s.answers['salary'],96000)
        self.assertEqual(s.pending,'confirm')
        self.assertIn('₹96,000',t.text)

    async def test_inline_multiple_corrections_update_together(self):
        s=CallFlow(answers={'purpose':'new','permission':True,'employment':True,
                            'requested_amount':90000,'salary':100000,'existing_emi':10000},
                   pending='confirm')
        t=await self.step(s, 'Both are not correct. My salary is twenty thousand and I need one lakh loan.')
        self.assertEqual(s.answers['salary'],20000)
        self.assertEqual(s.answers['requested_amount'],100000)
        self.assertEqual(s.pending,'confirm')
        self.assertIn('₹1 lakh',t.text)
        self.assertIn('₹20,000',t.text)

if __name__=='__main__':
    unittest.main()
