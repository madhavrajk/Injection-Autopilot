# Research: Visentia, the market, and where this prototype fits

Web research on 2026-11-18. Confidence: **[site]** read on visentia.ai; **[src]** from the linked source;
**[thin]** from a search summary or a vendor page and not checked further; **[none]** looked for and not found.

## 1. Visentia
- "AI-native operating system for the future of ophthalmology", **starting with retina**. Triple aim: access, quality, cost. [site]
- Product areas on the site: patient app (check-in, access), ambient scribe and automated charting, **scheduling agents
  "optimized for injection cadences"**, **care management agents** (prior authorization, insurance verification, care
  coordination), **RCM agents** (claims, "denials autopilot"), workflow automation across EMR, imaging and billing,
  imaging AI (OCT, fundus), and a **data flywheel**. [site]
- Approach: build the tech, partner with retina operators and deploy from within, then add-on acquisitions, clinical
  trials, licensing and value-based models. This reads like a technology-enabled practice platform, but the site does
  not say Visentia owns practices. [site]
- Founder and CEO: Vatsal Doshi, MD, MBA, MPH, vitreoretinal surgeon; founded a retina-only practice in New Jersey that
  was acquired by EyeCare Partners. [site] EyeCare Partners' Q2 2020 press release lists "Vitreous Retina Macula
  Specialists of New Jersey". [src: https://www.eyecare-partners.com/press-release/eyecare-partners-llc-announces-completion-of-six-new-practice-acquisitions-in-second-quarter/ ]
- Funding, investors, customers, pricing, press: [none]. Dave Contreras: [none] in public sources.
- Claims on the site worth knowing: 40% of physician time on admin; 20% overhead reduction; EBITDA margin 22% to 37%;
  US ophthalmology $100B by 2032; about 30% workforce gap by 2035; 82M Americans 65 or older by 2050. [site]

## 2. Who else is in the space
| Group | Players | What they do | Retina-specific? |
|---|---|---|---|
| Eye-care EHR incumbents | ModMed, Nextech, Barti, EVAA.AI | ModMed announced AI "assistants" (scribe live; billing, clinical, front office, patient planned for 2026) [src: https://businesswire.com/news/home/20251118161287/en/ModMed-Announces-Product-Roadmap-for-the-AI-Powered-Practice ]. Nextech launched Cora Scribe (Jan 2026) and partnered with Assort Health and EliseAI for patient access [thin]. Barti raised a $12M Series A for an AI-first eye-care EHR [src: https://www.ophthalmologytimes.com/view/barti-software-raises-12m-series-a-for-ai-powered-electronic-health-records-for-eye-care ] | General ophthalmology and optometry. Nothing found that is specific to the injection visit |
| Retina buy-and-bill | SamaCare, GenHealth AI, McKesson RetinaOS, AllyGPO / BioCareSD | SamaCare: prior authorization platform with a retina page [src: https://samacare.com/specialty-landing-page/retina-practice ]. McKesson RetinaOS: retina drug inventory and revenue management [src: https://www.mckesson.com/specialty/retina/retina-advanced-technology/ ]. GenHealth: AI prior authorization for ophthalmology [thin] | Yes, but each covers one slice (authorization, or inventory), and the distributors' tools come with a drug-purchasing relationship |
| Horizontal healthcare agents | Assort Health, Cohere Health, Tennr, Abridge, Ambience, AKASA and others | Assort: $120M Series C at $1.2B, AI patient access, already integrated with Nextech [src: https://hitconsultant.net/2026/06/24/assort-health-raises-120-million-series-c-menlo/ ]. Cohere: $90M Series C, payer-side authorization [src: https://www.beckershospitalreview.com/disruptors/cohere-health-raises-90m-5-notes/ ] | No. They are specialty-agnostic and well funded: Visentia should buy or integrate these, not rebuild them |
| Imaging AI | Topcon (acquired RetInSight), Optain (acquired EyePACS), Notal Vision, Altris and others | OCT analytics, screening, home monitoring [src: https://topconhealthcare.com/?p=10254 ] | Yes, but clinical, not operational |

## 3. The gap this prototype goes after
Each neighbour owns one checkpoint: authorization (SamaCare), inventory (RetinaOS), calls and scheduling (Assort),
claim correction (ModMed's planned billing assistant). **Nobody found ties the checkpoints to the one object a
retina practice actually runs on: the injection visit for one eye, one drug, one payer, on one date.** The same four facts
decide whether the eye is booked on time, whether the authorization covers it, whether the claim is right and
whether a denial is appealable. Handled by four vendors, those facts are re-keyed four times, and the errors live in
the seams. This is an absence of evidence from limited searching, not proof that nobody ships it.

Why it is the right wedge for Visentia specifically:
- It sits across three of the agents on Visentia's own site (scheduling for injection cadences, care management, RCM)
  and shows them as **one engine with three queues**, which is the "operating system" claim made concrete.
- The founder ran a retina practice. He will know in ten seconds whether the checks are the real ones.
- The money is concrete. Buy-and-bill is about 97% of anti-VEGF [thin: ASRS slides], the practice has already paid
  for the drug, and a denied dose is about $1,464 (Eylea) to $2,410 (Eylea HD) at Medicare limits [thin: derived from
  2026 ASP files via secondary copies].
- The pain is delay and rework, not rejection: about 96% of anti-VEGF authorizations are approved but about 60% are
  delayed more than 24 hours [src: https://www.houstonmethodist.org/leading-medicine-blog/articles/2026/jun/eye-opening-study-examines-impact-of-prior-authorization-requirements-on-retinal-care/ ].
  So the product has to look ahead (seven days of visits), not react.
- Adherence is a clinical problem a scheduler can fix: about 22% of nAMD patients had a gap of 12 months or more over
  four years (Obeid 2018) [thin: https://www.retinalphysician.com/issues/2021/june/loss-to-follow-up-after-anti-vegf-for-age-related-macular-degeneration-and-diabetic-retinopathy ].
- Every decision a coordinator makes (approve, edit, reject) is a label. That is the data flywheel on the site.

## 4. What Visentia's public story is missing, and what I would add
| Missing or thin on the site | What I would add | In this prototype? |
|---|---|---|
| The products are listed as separate agents with no shared object | One visit-level engine; agents are queues over it | Yes |
| No statement of how the agents are checked or when they stop | Zero-tolerance line, second-source check, blind evals, a person approves everything that leaves the practice | Yes (Trust page) |
| "Data flywheel" with no mechanism | Reviewer decisions become candidate eval cases; a person promotes them | Yes (`scripts/feedback_to_cases.py`) |
| HIPAA is a job requirement, not a product behaviour | Role-based masking on the server, redacted model prompts, a leak check that runs with the tests | Yes |
| Build versus buy is named but not shown | Buy the horizontal parts (voice, scribe, payer connectivity); build the retina logic and the cross-checks, which nobody sells | Stated in the walkthrough |
| No number for the customer | Dollars at risk before the patient is in the chair; eyes off schedule | Yes (Today) |
| Payer rule changes (CMS-0057-F: 7-day and 72-hour decisions from 2026, authorization API in 2027) | Policies as versioned data cited in every finding; ready for electronic authorization when payers expose it | Partly: versioned policy data, no live connection |

## 5. Market numbers (use with their tags)
- US digital health raised $14.2B in 2025; AI-enabled companies took 54%. [src: https://www.fiercehealthcare.com/digital-health/jpm26-digital-health-funding-hit-142b-2025-ai-companies-taking-lions-share-dollars ]
- Medicare Part B anti-VEGF spending grew from $2.51B (2014) to $4.02B (2019), across 17.6M claims and 4,218 ophthalmologists. [thin: https://eye.hms.harvard.edu/publications/trends-anti-vascular-endothelial-growth-factor-original-medicare-part-b-claims ]
- Private equity: about 29% of retina specialists were PE-affiliated by 2022 in states with PE presence [thin]; Cencora agreed to buy EyeSouth's retina business for $1.1B in March 2026 [thin: https://www.beckersasc.com/?p=106909 ].
- About 2,100 to 2,200 US members in ASRS's 2020 survey; a count of US retina practices was not found. [thin / none]
- Denial rework cost: MGMA $25.20 per claim; HFMA $25 to $118. [thin] Retina-specific denial rate: [none].
