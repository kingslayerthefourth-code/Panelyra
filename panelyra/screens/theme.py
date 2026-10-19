"""Shared, accessible visual tokens for the native Streamlit interface."""
import streamlit as st

def inject_theme():
    st.markdown('''<style>
    :root {--accent:#57d5c5;--text:#e9eeef;--muted:#aebcbe;--surface:#151c20;--border:#354348}
    .stApp {background:#0e1417!important;color:var(--text)!important}
    [data-testid="stMainBlockContainer"] {max-width:1180px;padding:2.5rem 2rem 4rem!important}
    [data-testid="stSidebar"] {background:#11191d!important;border-right:1px solid #29373c}
    .brand-name{font-size:1.5rem!important;letter-spacing:-.04em}
    .brand-tagline{color:#aebcbe!important;font-size:.78rem!important}
    .app-shell{background:none!important;border:0!important;box-shadow:none!important;padding:0 0 1rem!important;margin:0!important}
    .page-title{font-size:2rem!important;line-height:1.2!important;letter-spacing:-.04em!important;font-weight:650!important;color:#edf4f3!important;padding:0!important}
    .page-subtitle{font-size:.95rem!important;color:#aebcbe!important;line-height:1.5!important;margin:.5rem 0 0!important}
    h2{font-size:1.4rem!important}h3{font-size:1.15rem!important}
    .pill-row,.global-bar{display:none!important}
    [data-testid="stMetricValue"]{font-size:1.35rem!important}
    [data-testid="stMetric"],.panel,.lyra-welcome,.lyra-full-shell {background:#151c20!important;box-shadow:none!important}
    [data-testid="stCaptionContainer"]{color:#aebcbe!important}
    input,textarea,[data-baseweb="select"]>div{background:#182227!important;color:#edf4f3!important;border-color:#415158!important}
    input::placeholder,textarea::placeholder{color:#9aabad!important;opacity:1!important}
    button[kind="primary"],button[kind="primaryFormSubmit"],a[kind="primary"]{background:#57d5c5!important;border-color:#57d5c5!important;color:#092822!important;box-shadow:none!important}
    button[kind="secondary"],button[kind="secondaryFormSubmit"],a[kind="secondary"]{background:#1b272c!important;border:1px solid #415158!important;color:#e9eeef!important;box-shadow:none!important}
    button[kind] p,a[kind] p{color:inherit!important}
    button[kind]:disabled{background:#263339!important;border-color:#415158!important;color:#aab6ba!important;opacity:1!important}
    [data-testid="stSidebar"] button[kind="secondary"]{background:transparent!important;border-color:transparent!important;color:#b9c7c9!important}
    [data-testid="stSidebar"] button[kind="primary"]{background:#183d38!important;border-color:#377e73!important;color:#89edde!important}
    button:focus-visible,a:focus-visible,input:focus-visible,textarea:focus-visible,[tabindex]:focus-visible{outline:3px solid #79e5d6!important;outline-offset:3px!important}
    [role="tooltip"]{background:#e3efed!important;color:#112925!important}
    [role="tooltip"] *{color:#112925!important}
    [data-baseweb="tab-highlight"]{background:#57d5c5!important}
    [aria-selected="true"]{color:#85e6d8!important}
    .library-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:20px;margin:8px 0 24px}
    .library-book{display:flex;flex-direction:column;gap:7px;min-width:0;padding:10px;border:1px solid #334247;border-radius:12px;background:#151e22;color:#e9eeef!important;text-decoration:none!important;transition:border-color .15s}
    .library-book:hover,.library-book.selected{border-color:#57d5c5;background:#1a2a2b}
    .library-book img,.book-placeholder{width:100%;aspect-ratio:3/4;object-fit:cover;border-radius:6px;background:#233338}
    .book-placeholder{display:grid;place-content:center;color:#75afa6;font-size:3rem}
    .library-book strong{font-size:.88rem;line-height:1.4;overflow-wrap:anywhere}
    .library-book small{color:#aebcbe;font-size:.78rem}
    .lyra-orb{background:#214c46!important;box-shadow:none!important;animation:none!important}
    section[data-testid="stSidebar"] button[kind="primary"]{background:#183d38!important;background-image:none!important;border:1px solid #377e73!important;box-shadow:inset 3px 0 #57d5c5!important;color:#b2f4e9!important}
    section[data-testid="stSidebar"] button[kind="secondary"]{background:transparent!important;background-image:none!important}
    .st-key-workspace_views button[kind="primary"]{background:transparent!important;color:#89edde!important;border:1px solid #377e73!important;box-shadow:none!important}
    [data-testid="stExpander"] details{background:#151f24!important;background-image:none!important}
    [role="tooltip"] *,[data-baseweb="tooltip"] *{color:#112925!important;background-color:#e3efed!important;background-image:none!important}
    .st-key-lyra_thread_rail{background:#151f24!important;box-shadow:none!important}
    .st-key-workspace_views [role="radiogroup"]{display:flex!important;flex-wrap:wrap;gap:3px!important;width:fit-content;max-width:100%;padding:4px;border:1px solid #34464c;border-radius:10px;background:#141f24}
    .st-key-workspace_views [role="radiogroup"] label{position:relative;min-height:34px!important;padding:6px 18px!important;margin:0!important;border-radius:6px!important;background:transparent;color:#aabfc3!important;cursor:pointer}
    .st-key-workspace_views [role="radiogroup"] label>div:first-child{position:absolute!important;opacity:0!important;width:1px!important;height:1px!important;overflow:hidden}
    .st-key-workspace_views [role="radiogroup"] label:has(input:checked){background:#29463f!important;color:#c2f4e9!important}
    .st-key-workspace_views [role="radiogroup"] label:focus-within{outline:2px solid #79e5d6;outline-offset:2px}
    .st-key-workspace_views [role="radiogroup"] label:hover{background:#26373c}
    /* Button system: clear hierarchy, stable hit areas, no gradients or glow. */
    .stApp button[kind],.stApp [data-testid="stLinkButton"] a{min-height:42px!important;border-radius:8px!important;padding:9px 17px!important;line-height:1.2!important;font-weight:550!important;box-shadow:none!important;background-image:none!important;transition:background-color .12s,border-color .12s!important;transform:none!important}
    .stApp button[kind]::before,.stApp button[kind]::after{display:none!important}
    .stApp button[kind="primary"]:not(:disabled):hover,.stApp button[kind="primaryFormSubmit"]:not(:disabled):hover,.stApp [data-testid="stLinkButton"] a[kind="primary"]:hover{background:#82e5d6!important;border-color:#82e5d6!important}
    .stApp button[kind="secondary"]:not(:disabled):hover,.stApp [data-testid="stLinkButton"] a[kind="secondary"]:hover{background:#29383e!important;border-color:#728a90!important}
    .stApp button[kind]:active{filter:brightness(.94)}
    .stApp button[kind]:disabled{cursor:not-allowed!important;color:#a9b8bc!important;background:#202b30!important;border-color:#3b4b52!important}
    section[data-testid="stSidebar"] button[kind]{min-height:42px!important;padding:10px 13px!important;border-radius:7px!important;font-size:.92rem!important}
    section[data-testid="stSidebar"] button[kind][kind="primary"]{background:#183d38!important;border:1px solid #37655f!important;color:#adf1e6!important;box-shadow:none!important}
    section[data-testid="stSidebar"] button[kind][kind="secondary"]:hover{background:#1c2a2f!important;border-color:#3a5057!important}
    .st-key-workspace_views [data-testid="stButtonGroup"]{width:fit-content!important;max-width:100%;padding:4px;border:1px solid #34464c;border-radius:10px;background:#141f24}
    .st-key-workspace_views button{min-height:34px!important;padding:7px 18px!important;border-radius:6px!important;border:0!important;background:transparent!important;color:#aabfc3!important;font-size:.86rem!important;box-shadow:none!important}
    .st-key-workspace_views button[aria-pressed="true"]{background:#29463f!important;color:#c2f4e9!important}
    .st-key-workspace_views button:hover{background:#26373c!important;color:#ecf4f3!important}
    [data-testid="stSidebarCollapsedControl"] button{min-width:42px;min-height:42px;border-radius:8px!important;background:#18272c!important;color:#c5efea!important}
    .stApp section[data-testid="stSidebar"] button[kind][kind="primary"]:not(:disabled):hover{background:#24534b!important;border-color:#498c80!important;color:#c4f8ed!important}
    .stApp section[data-testid="stSidebar"] button[kind][kind="primary"]:not(:disabled):hover p{color:#c4f8ed!important}
    @media(max-width:640px){[data-testid="stMainBlockContainer"]{padding:1.25rem 1rem 3rem!important}.page-title{font-size:1.65rem!important}.library-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}[data-testid="stHorizontalBlock"]{flex-wrap:wrap!important}[data-testid="stColumn"]{min-width:min(100%,160px)!important}button,a{overflow-wrap:anywhere}.lyra-full-shell{padding:0!important}}
    .stApp button[kind="primary"] [data-testid="stIconMaterial"]{color:#12312c!important}
    @media(max-width:640px){[data-testid="stSidebarCollapseButton"]{position:fixed!important;top:4px!important;left:12px!important;z-index:999999!important}[data-testid="stHeader"]{display:block!important;background:#10191e!important;height:3rem!important}[data-testid="stMainBlockContainer"]{padding-top:3.5rem!important}}
    @media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
    </style>''',unsafe_allow_html=True)
