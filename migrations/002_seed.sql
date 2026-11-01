-- Idempotent seed: playbooks, stages, script hints, policies.
-- ASSUMPTION: conversion_value_inr=500 and optout_penalty_inr=200 are demo defaults
-- editable via PUT /policy. daily_budget_inr=500 likewise.

INSERT INTO playbooks (key, label, agent_id_env_var, active) VALUES
    ('reactivation', 'Reactivation (no referral)', 'BOLNA_AGENT_ID_REACTIVATION', true),
    ('referral',     'Referral invitee',           'BOLNA_AGENT_ID_REFERRAL',     true)
ON CONFLICT (key) DO NOTHING;

-- Stages: key | label | goal | threshold_minutes | ordinal
-- Same stage definitions for both playbooks (thresholds identical unless policy changes later).
INSERT INTO playbook_stages (playbook_key, stage_key, ordinal, label, goal, threshold_minutes) VALUES
    ('reactivation', 'signup',  1, 'phone verification',   'help them finish verifying their phone number', 10),
    ('reactivation', 'profile', 2, 'profile setup',        'help them finish their profile details',        20),
    ('reactivation', 'kyc',     3, 'ID verification',      'help them finish uploading their ID',           30),
    ('reactivation', 'bank',    4, 'bank account linking', 'help them link their bank account',             20),
    ('reactivation', 'plan',    5, 'choosing a plan',      'help them choose a plan and confirm it',        15),
    ('reactivation', 'deposit', 6, 'first deposit',        'help them complete their first deposit',        10),
    ('referral',     'signup',  1, 'phone verification',   'help them finish verifying their phone number', 10),
    ('referral',     'profile', 2, 'profile setup',        'help them finish their profile details',        20),
    ('referral',     'kyc',     3, 'ID verification',      'help them finish uploading their ID',           30),
    ('referral',     'bank',    4, 'bank account linking', 'help them link their bank account',             20),
    ('referral',     'plan',    5, 'choosing a plan',      'help them choose a plan and confirm it',        15),
    ('referral',     'deposit', 6, 'first deposit',        'help them complete their first deposit',        10)
ON CONFLICT (playbook_key, stage_key) DO NOTHING;

-- Policies (ASSUMPTIONS marked in comments above). Separate rows per playbook.
INSERT INTO policies (
    playbook_key, min_score, daily_budget_inr,
    call_window_start, call_window_end, timezone,
    per_user_cap, per_user_cap_days, holdout_pct,
    conversion_value_inr, optout_penalty_inr, reward_window_hours
) VALUES
    ('reactivation', 0.35, 500, '10:00', '19:00', 'Asia/Kolkata', 2, 7, 0.10, 500, 200, 48),
    ('referral',     0.35, 500, '10:00', '19:00', 'Asia/Kolkata', 2, 7, 0.10, 500, 200, 48)
ON CONFLICT (playbook_key) DO NOTHING;

-- Script hints: 3 calm, helpful hints per stage per playbook.
-- No investment advice, return promises, or pressure. Referral may mention the friend's invite.

-- ===== reactivation hints =====
INSERT INTO script_hints (playbook_key, stage_key, hint_index, text) VALUES
('reactivation','signup',1,'Hi, this is Sprout. We noticed you started signing up — we can stay on the line while you finish verifying your phone, if that helps.'),
('reactivation','signup',2,'Hello from Sprout. Phone verification can sometimes take a moment. I am here if you would like a calm walkthrough.'),
('reactivation','signup',3,'Hi, Sprout here. Whenever you are ready, we can help you complete the phone check so you can continue at your own pace.'),

('reactivation','profile',1,'Hi, this is Sprout. Your profile details are the next step — I can answer simple questions while you fill them in.'),
('reactivation','profile',2,'Hello from Sprout. Setting up your profile is quick. I am here if anything on the form is unclear.'),
('reactivation','profile',3,'Hi, Sprout here. We can stay with you while you finish your profile details — no rush.'),

('reactivation','kyc',1,'Hi, this is Sprout. ID verification is next. I can explain what you will need, without collecting any ID over the phone.'),
('reactivation','kyc',2,'Hello from Sprout. Uploading your ID can feel fiddly on mobile — I am here if you want a gentle checklist.'),
('reactivation','kyc',3,'Hi, Sprout here. Whenever you are ready to finish ID verification, I can guide you through the on-screen steps.'),

('reactivation','bank',1,'Hi, this is Sprout. Linking a bank account is the next step. I can talk you through the buttons you will see in the app.'),
('reactivation','bank',2,'Hello from Sprout. Bank linking stays inside the app for your safety. I am here if the screen is confusing.'),
('reactivation','bank',3,'Hi, Sprout here. We can stay on the line while you connect your bank — take your time.'),

('reactivation','plan',1,'Hi, this is Sprout. Choosing a plan is next. I can help you understand what each option means, without recommending one.'),
('reactivation','plan',2,'Hello from Sprout. If you paused on the plan screen, I am happy to walk through the labels calmly.'),
('reactivation','plan',3,'Hi, Sprout here. Confirming a plan is your choice — I can stay with you while you review the options.'),

('reactivation','deposit',1,'Hi, this is Sprout. Completing a first deposit is the last signup step. I can explain what you will see, without urging you to fund.'),
('reactivation','deposit',2,'Hello from Sprout. If you paused before depositing, that is fine. I am here for a calm walkthrough of the screen.'),
('reactivation','deposit',3,'Hi, Sprout here. Whenever you decide to continue, we can help you complete the deposit steps in the app.')
ON CONFLICT (playbook_key, stage_key, hint_index) DO NOTHING;

-- ===== referral hints (may mention the friend's invite) =====
INSERT INTO script_hints (playbook_key, stage_key, hint_index, text) VALUES
('referral','signup',1,'Hi, this is Sprout. Your friend invited you — we can help you finish verifying your phone whenever you are ready.'),
('referral','signup',2,'Hello from Sprout. Thanks for joining via an invite. Phone verification is next; I am here if you would like a calm walkthrough.'),
('referral','signup',3,'Hi, Sprout here. Your invite is saved. We can stay on the line while you complete the phone check at your own pace.'),

('referral','profile',1,'Hi, this is Sprout. Your friend pointed you here — next is your profile. I can answer simple questions while you fill it in.'),
('referral','profile',2,'Hello from Sprout. Finishing your profile keeps your invite linked. I am here if anything on the form is unclear.'),
('referral','profile',3,'Hi, Sprout here. No rush on the profile details — we can walk through them together after your friend''s invite.'),

('referral','kyc',1,'Hi, this is Sprout. After your friend''s invite, ID verification is next. I can explain what you will need, without collecting ID over the phone.'),
('referral','kyc',2,'Hello from Sprout. Uploading your ID can feel fiddly — I am here with a gentle checklist so your invite signup can continue.'),
('referral','kyc',3,'Hi, Sprout here. Whenever you are ready, we can guide you through the ID steps your friend''s invite unlocked.'),

('referral','bank',1,'Hi, this is Sprout. Linking a bank account is next on your invited signup. I can talk you through the in-app buttons.'),
('referral','bank',2,'Hello from Sprout. Bank linking stays inside the app. Your friend''s invite is already noted — I am here if the screen is confusing.'),
('referral','bank',3,'Hi, Sprout here. We can stay on the line while you connect your bank after joining via invite — take your time.'),

('referral','plan',1,'Hi, this is Sprout. Choosing a plan is next. I can help you understand the labels — your friend''s invite does not change that it is your choice.'),
('referral','plan',2,'Hello from Sprout. If you paused on the plan screen after joining via invite, I am happy to walk through the options calmly.'),
('referral','plan',3,'Hi, Sprout here. Confirming a plan is entirely up to you. We can review the screen together at an easy pace.'),

('referral','deposit',1,'Hi, this is Sprout. A first deposit is the last step after your friend''s invite. I can explain the screen without urging you to fund.'),
('referral','deposit',2,'Hello from Sprout. Pausing before a deposit is fine. Your invite stays linked — I am here for a calm walkthrough.'),
('referral','deposit',3,'Hi, Sprout here. Whenever you decide to continue, we can help you complete the deposit steps after joining via invite.')
ON CONFLICT (playbook_key, stage_key, hint_index) DO NOTHING;
