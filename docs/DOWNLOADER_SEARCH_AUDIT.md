# Downloader search repair — 11 October 2026

Search now uses 27 source-specific integrations, up from three. 26 returned matching titles in live checks; MangaDex could not be reached and is opt-in. The old implementation assumed a WordPress search URL and a one-segment series path for Kayn; its current site uses `/series?q=…` and `/series/comic/<slug>`. Asura uses `/browse?q=…`, WeebCentral uses `/search/data`, and other adapters use their observed query forms. Thunder now uses its site's plain `s` query; both the old and new parameters returned results during today's recheck, so its earlier empty results were not conclusively a parameter bug.

Results require all query words, exact title matches rank first, chapter links and unrelated hosts are excluded, referral URLs are deduplicated, and long result sets are paginated with a source filter. Same-source HTTPS redirects are followed; cross-host moves require an adapter update. Access challenges, rate limits and unknown markup are reported separately from empty searches.

## Validation

- All 27 production adapters queried live, with individual counts and timings in `outputs/downloader-check/2026-10-11/final-live-report.json`.
- Asura Solo Leveling chapter 1 downloaded successfully: 22 image pages verified with Pillow, archive size 4,409,747 bytes. Test archive stays under outputs; no library import or duplicate created.
- Existing title-search and downloads UI regressions passed. Seven additional regressions cover nested series paths, exact query matching, canonical redirects, challenges, rate limits, pagination and selection.
- Search verification is not a full download test for every source. Availability, free chapter coverage and layouts can change. No login, payment or challenge bypass added.

## Integrated search

| Source | Live matches | Check |
|---|---:|---|
| Asura | 2 | Returned titles |
| Thunder | 8 | Returned titles |
| Kayn | 1 | Returned titles |
| MangaPill | 6 | Returned titles |
| WeebCentral | 3 | Returned titles |
| MangaRead | 12 | Returned titles |
| MangaSushi | 1 | Returned titles |
| LHTranslation | 2 | Returned titles |
| ManhuaPlus | 5 | Returned titles |
| Rolia | 20 | Returned titles |
| GalaxyDegen | 8 | Returned titles |
| MangaTaro | 20 | Returned titles |
| Drake | 1 | Returned titles |
| Tritinia | 1 | Returned titles |
| Athrea | 5 | Returned titles |
| Eva | 3 | Returned titles |
| Lagoon | 10 | Returned titles |
| Madara | 10 | Returned titles |
| Raven | 20 | Returned titles |
| Rokari | 6 | Returned titles |
| Scythe | 2 | Returned titles |
| Yaksha Comics | 3 | Returned titles |
| Violet | 2 | Returned titles |
| Mist | 1 | Returned titles |
| Rithar | 17 | Returned titles |
| MangaKatana | 2 | Returned titles |
| MangaDex | 0 | Source unavailable or returned an invalid response. Retry or use Paste link. |

## Additional requested sites

The table below records the supplied URL checks. HTTP 200 only proves a page responded; it does not prove downloader compatibility. Sites without a verified search adapter remain outside integrated search. The installed upstream downloader's broader source directory is exposed under Paste link, explicitly labelled as upstream support rather than a live success guarantee.

- Bato, Falcon, MangaPark.io and Weebdex were not enabled: the supplied list marks them shut down. The upstream MangaPark adapter lists a different host.
- Arven's supplied domain redirected toward a parked HTTP domain; UTOON.net serves games, so neither was added. Upstream's UTOON adapter uses utoon.us.
- Eva, Madara and Violet search use their observed current hosts (`evascans.net`, `madarascans.net`, `violetmanga.com`). Yaksha Scans redirected to Raven; Raven is included once.
- MangaYY search was blocked; KS Group did not expose compatible chapter rows in the checked response; Writers search did not return a verified match. They were not added to integrated search.
- The later name-only list was used as optional candidates. Overlapping integrations and upstream-supported providers are covered. Ambiguous names, unverified replacements, and paid-platform-specific integrations (Tapas/Tappytoon/MangaToon) were not added or presented as working. MangaUpdates is not treated as a chapter download source.

| Requested source | Supplied URL | Observed response |
|---|---|---|
| Arven Comics | https://arvencomics.com | 302 http://ww19.arvencomics.com/  |
| Arya Scans | https://brainrotcomics.com | 403 https://brainrotcomics.com/ Just a moment... |
| Asmodeus Scans | https://asmotoon.com | 403 https://asmotoon.com/ Just a moment... |
| Athrea Scans | https://athreascans.com | 200 https://athreascans.com/ Athrea Scans |
| AtsuMoe | https://atsumoe.com | 000 https://atsumoe.com/  |
| Bato | https://bato.to | Not enabled; listed as shutdown by user |
| Comix | https://comix.to | 200 https://comix.to/ Comix - Read Comics online for free |
| DemonicScans | https://demonicscans.org | 200 https://demonicscans.org/ Read Manga At demonicscans |
| Diva Scans | https://divatoon.com | 200 https://divascans.org/ DivaScans — Read Novels, Manhwa & Comics Online Free |
| Elf Toon | https://elftoon.com | 200 https://elftoon.net/ Elf Toon — Read Manhwa & Comics Online Free |
| Eva Scans | https://evascans.org | 200 https://evascans.net/ Eva Scans – Read the Best Romance Manhwa and Manga Online – Free, Fast, Updated Daily |
| Falcon Scans | https://falconscans.com | Not enabled; listed as shutdown by user |
| Firescans | https://firescans.xyz | 000 https://firescans.xyz/  |
| FlameComics | https://flamecomics.xyz | 200 https://flamecomics.xyz/ Flame Comics - Read Manga, Manhwa & Manhua Free |
| Greed Scans | https://greedscans.com | 301 https://greedscans.org/  |
| Hades Scans | https://hadesscans.com | 200 https://hadesscans.com/ Hades Scans is under maintenance |
| KaliScan | https://kaliscan.com | 000 https://kaliscan.com/  |
| KappaBeast | https://kappabeast.com | 200 https://kappabeast.com/ KappaBeast - Read Manga Online |
| KDT Scans | https://www.silentquill.net | 200 https://silentquill.net/ SilentQuill |
| Kenscans | https://kencomics.com | 200 https://kencomics.com/ Kenscans / English Manga & Manhwa Translations - Latest Chapters - Lose yourself in a new dimension of comics, manga, manhua, and manhwa — Ken: Where stories come to life. |
| KS Group | https://ksgroupscans.com | 200 https://ksgroupscans.com/home-site/ HOME SITE – KS Scans |
| KuraManga | https://kuramanga.com | 200 https://kuramanga.com/ KuraManga - Free Manga, Manhwa & Webtoons Reader |
| LagoonScans | https://lagoonscans.com | 200 https://lagoonscans.com/ Lagoon Scans - Read Manhwa & manhua Online Free  |
| LikeManga | https://likemanga.io | 000 https://likemanga.io/  |
| Lua Comic | https://luacomic.org | 200 https://luacomic.org/ Lua Comic |
| Madarascans | https://madarascans.com | 200 https://madarascans.net/ Madarascans - Read the Best Manga, Manhwa, Manhua & Comics Online – Free English Scans |
| Magus Manga | https://magustoon.org | 403 https://magustoon.org/ Just a moment... |
| MangaCloud | https://mangacloud.org | 200 https://mangacloud.org/ MangaCloud |
| Mangago | https://www.mangago.zone | 200 https://www.mangago.zone/ Read comics online for free - Mangago |
| MangaKatana | https://mangakatana.com | 200 https://mangakatana.com/ MangaKatana - Read Manga Online |
| Mangaloom | https://mangaloom.com | 000 https://mangaloom.com/  |
| MangaPark | https://mangapark.io | Not enabled; listed as shutdown by user |
| MangaYY | https://mangayy.org | 200 https://mangayy.org/ MangaYY – Read Free Manga – Manhwa – Manhua Online – MangaYY.Org |
| Manhuaplus | https://manhuaplus.top | 200 https://manhuaplus.top/ Manhuaplus - Read Manga Fastest and highest quality updates |
| ManhuaUS | https://manhuaus.com | 403 https://manhuaus.com/ Just a moment... |
| Mgeko | https://mgeko.cc | 200 https://www.mgeko.cc/ www.mgeko.cc - Enjoy reading |
| Mist Scans | https://mistscans.com | 200 https://mistscans.com/ Mist Scans |
| NovelCool | https://www.novelcool.com | 200 https://www.novelcool.com/ Novel Cool - Best online light novel reading website |
| Philia Scans | https://philiascans.org | 200 https://philiascans.org/ Philia Scans — Read Manga Online For Free |
| Project Suki | https://projectsuki.com | 200 https://projectsuki.com/ Project Suki - Your next favorite read is just a click away |
| Qi Scans | https://qimanhwa.com | 200 https://qimanga.com/ Qi Manga |
| Rage Scans | https://ragescans.com | 403 https://ragescans.com/ Just a moment... |
| Raven Scans | https://ravenscans.org | 200 https://ravenscans.org/ Raven Scans – The Fastest Manga Updates |
| RD Scans | https://rdscans.com | 200 https://rdscans.com/  |
| Ritharscans | https://ritharscans.com | 200 https://ritharscans.com/ Ritharscans - Read English Manga & Manhwa Translations |
| Rizz Fables | https://rizzfables.com | 200 https://rizzfables.com/ Rizz Fables |
| Rokari Comics | https://rokaricomics.com | 200 https://rokaricomics.com/ rokari comics – Immerse Yourself in Comics, Manga, Manhua, and Manhwa — Rokari: Where Stories Come to Life. |
| Scythe Scans | https://scythescans.com | 200 https://scythescans.com/ Scythe Scans - Home |
| Spider Scans | https://spiderscans.xyz | 200 https://spiderscans.xyz/ SpiderScans – Best Site To Read Famous Series ! |
| Stonescape | https://stonescape.xyz | 200 https://stonescape.xyz/ StoneScape |
| Temple Scan | https://templetoons.com | 000 https://templetoons.com/  |
| TopManhua | https://manhuatop.org | 403 https://manhuatop.org/ Just a moment... |
| UTOON | https://utoon.net | 200 https://utoon.net/ UTOON Games — Browser Games |
| Violetscans | https://violetscans.org | 200 https://violetmanga.com/ Violet Scans / Read Romance Manga, Manhwa & Comics Online |
| Vortex Scans | https://vortexscans.org | 200 https://vortexscans.org/ Vortex Scans - Read Comics, manga, manhua, manhwa, translated swiftly: Vortex, your ultimate library. |
| WEBTOON | https://www.webtoons.com | 200 https://www.webtoons.com/en/ WEBTOON - Read Comics, Manga & Manhwa |
| Weebdex | https://weebdex.org | Not enabled; listed as shutdown by user |
| WitchScans | https://witchscans.com | 000 https://witchscans.com/  |
| Writers' Scans | https://writerscans.com | 200 https://writerscans.com/ Writers' Scans |
| Yaksha Comics | https://yakshacomics.com | 200 https://yakshacomics.com/ YakshaComics - The Briskest Scans |
| Yaksha Scans | https://yakshascans.com | 200 https://ravenscans.org/ Raven Scans – The Fastest Manga Updates |

## Chapter counts and source priority

Each search result and selected-title panel now displays a chapter count. Exact title matches rank first; within that, higher chapter counts rank higher and unavailable counts rank last. Site-reported totals are distinguished internally from counted chapter links; partial lists are labelled “At least N chapters”. Totals can include locked chapters, so they are not a promise that every chapter can be downloaded. Highest chapter numbers are never mistaken for counts. Duplicate chapter numbers, including repeated responsive layouts, are deduplicated; gaps and decimal chapters are counted as entries.

Counts are fetched concurrently (maximum eight workers, two requests per host), bounded by response size and timeouts, and cached for ten minutes. Progress appears while counts load. Failed lookups retain the search result with “Chapter count unavailable”.

Verified example: Happy Face on Kayn lists 78 chapters; MangaTaro lists 52. Kayn is ranked first. Eight chapter-count regressions cover reported totals, animated number widgets, partial lists, gaps, deduplication, unknown counts, source ordering, caching and WeebCentral’s full-list endpoint.
