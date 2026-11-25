# Domain recon: what the rules rest on, and how sure each one is

Scouted before any code. **Verified** = read in a primary or authoritative source during recon. **Secondary** = vendor
page, search summary or a copy of a CMS file. **Assumed** = general coding knowledge that was not re-verified; a
retina biller should confirm it before any of this touches a real claim.

| Rule in the prototype | Basis | Confidence |
|---|---|---|
| CPT 67028 for an intravitreal injection; RT / LT per eye | Retina Today "How to create clean claims" (2025); NGS article A53387 | Verified |
| J0178 Eylea, 1 mg per unit, 2 units per dose | A53387 | Verified |
| J2777 Vabysmo, 0.1 mg per unit, 60 units per 6 mg | Retina Today | Verified |
| J0177 Eylea HD, 1 mg per unit, 8 units per 8 mg | CMS JW/JZ code list; 8 units is implied, not read | Secondary |
| J2778 Lucentis, 0.1 mg per unit, 5 units per 0.5 mg | CMS code lists via secondary copies | Secondary |
| Bevacizumab code depends on the payer (J7999, C9257, J3590, J3490); J9035 is wrong for a 1.25 mg dose | Noridian and NGS articles via AAO; payer pages | Secondary. Modelled as policy data, never hard-coded |
| JZ when nothing is discarded from a single-dose vial; JW when one unit or more is | AAO EyeNet "Meet modifier JZ" | Verified in outline |
| NDC: 11 digits, 5-4-2 | Retina Today | Verified |
| Modifier 25 on a same-day E/M or eye visit code | Standard coding rule | Assumed |
| Diagnosis laterality should agree with the eye modifier | Coding consistency; no payer source found | Assumed. Shown as a consistency check |
| ICD-10 codes in `DX_CODES` | ICD-10-CM structure from memory | Assumed. Ten codes only; anything else is "not recognised", never guessed |
| 28-day minimum interval, same eye | Payer edits reported by ASRS; A53387 removed its own 4-week rule | Secondary. A per-payer policy value |
| Medicare Advantage may require bevacizumab first (since 2019) | CMS fact sheet, Aug 2018 | Verified |
| Traditional Medicare needs no prior authorization for anti-VEGF | General knowledge | Assumed |
| CARC meanings (197, 4, 11, 16, 151, 50, 27, 29) | X12 code list from memory | Assumed |
| Allowed amounts per dose | Derived from 2026 ASP limits via secondary copies (Eylea, Eylea HD); the rest are placeholders | Illustrative only |
| SNOMED right eye 8966001, left eye 18944008 | From memory | Assumed |

## Not modelled, on purpose
- Bilateral billing with modifier 50 (varies by payer and MAC).
- Same-day anti-VEGF plus a complement inhibitor (Syfovre, Izervay). Every MAC denied these under an unpublished
  28-day edit in 2025 to 2026 and some agreed to reprocess; it is a good next rule and a good example of why policies
  must be data.
- Biosimilars (Pavblu Q5147, Byooviz Q5124, Cimerli Q5128), brolucizumab, wastage units with JW, drug inventory.
- Label-specific loading doses and maximum intervals. The plan's interval is taken from the order as written.

## How the data would arrive in a real practice
| Need | Standard | Notes |
|---|---|---|
| Appointments, orders, procedures, drug administration, notes | FHIR (Appointment, MedicationRequest, Procedure, MedicationAdministration, DocumentReference) | ModMed: proprietary API plus certified FHIR R4 (read, search, bulk). Nextech Select API is FHIR STU3-based. Partner approval and sandbox access take weeks |
| Eligibility | X12 270/271, or FHIR CoverageEligibilityRequest/Response | Through a clearinghouse |
| Prior authorization | X12 278; Da Vinci PAS/CRD/DTR | CMS-0057-F requires payer authorization APIs from 2027; whether Part B drugs are in scope was not confirmed |
| Claims and remittances | X12 837P and 835; FHIR Claim / ClaimResponse | CARC and RARC codes arrive in the 835 |

The prototype reads one FHIR R4 bundle in these resource shapes (mapping in `docs/spec.md` section 6), so the ingest
module is the only part that changes when a real source is connected.
