import random
import re
import time
import unicodedata

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from .navigation import open_or_rotate, pop_last_graph_retry_after
from .utils import jitter_short


def click_all_see_more(driver, scope):
    texts = ["see more", "show more", "read more", "+ more", "ver más", "mostrar más", "cargar más"]
    try:
        buttons = scope.find_elements(By.XPATH, ".//button|.//a")
        for b in buttons:
            label = (b.text or b.get_attribute("aria-label") or "").strip().lower()
            if any(t in label for t in texts):
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", b)
                    jitter_short()
                    try:
                        b.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", b)
                    time.sleep(0.2 + random.random() * 0.2)
                except Exception:
                    continue
    except Exception:
        pass


def extract_campaign_text_and_media_counts(driver):
    def normtxt(s):
        if not s:
            return ""
        s = s.replace(" ", " ")
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        s = re.sub(r"\n{3,}", "\n\n", s)
        s = "\n".join([ln.strip() for ln in s.split("\n")])
        return unicodedata.normalize("NFC", s)

    story_scope = None
    for sel in ["div.col.col-12.grid-col-9-lg.z1", ".px3"]:
        try:
            story_scope = driver.find_element(By.CSS_SELECTOR, sel)
            if story_scope:
                break
        except Exception:
            story_scope = None

    if story_scope is None:
        for sel in [".story-content", ".story-content .rte__content", "body"]:
            try:
                story_scope = driver.find_element(By.CSS_SELECTOR, sel)
                if story_scope:
                    break
            except Exception:
                continue
    if story_scope is None:
        story_scope = driver.find_element(By.TAG_NAME, "body")

    try:
        click_all_see_more(driver, story_scope)
    except Exception:
        pass

    try:
        story_text = driver.execute_script("return arguments[0].innerText || '';", story_scope)
    except Exception:
        story_text = ""
    story_text = normtxt(story_text)

    if not story_text:
        parts = []
        xpaths = [
            ".//h1", ".//h2", ".//h3", ".//h4", ".//h5", ".//h6",
            ".//p", ".//*[contains(@class,'ace-line')]",
            ".//li", ".//figcaption", ".//blockquote", ".//pre"
        ]
        for xp in xpaths:
            try:
                for el in story_scope.find_elements(By.XPATH, xp):
                    t = normtxt(el.text or "")
                    if not t:
                        continue
                    tag = el.tag_name.lower().strip()
                    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                        parts.append("\n\n" + t)
                    elif tag == "li":
                        parts.append("• " + t)
                    else:
                        parts.append(t)
            except Exception:
                pass
        story_text = normtxt("\n".join([p for p in parts if p]).strip())

    image_count = 0
    try:
        figs = story_scope.find_elements(By.CSS_SELECTOR, "figure.image")
        image_count = len([f for f in figs if f.is_displayed()])
    except Exception:
        image_count = 0

    video_count = 0
    try:
        native_videos = story_scope.find_elements(By.TAG_NAME, "video")
        iframes = story_scope.find_elements(By.TAG_NAME, "iframe")
        iframe_like_videos = 0
        for fr in iframes:
            try:
                src = (fr.get_attribute("src") or "").lower()
            except Exception:
                src = ""
            if any(k in src for k in ["youtube.com", "youtu.be", "vimeo.com", "player.vimeo.com",
                                       "embedly", "wistia", "kck.st/video"]):
                iframe_like_videos += 1
        embed_divs = story_scope.find_elements(By.CSS_SELECTOR, "div.text-center.clip.mb5, .video, .embedded-video")
        video_count = (len([v for v in native_videos if v.is_displayed()])
                       + iframe_like_videos
                       + len([e for e in embed_divs if e.is_displayed()]))
    except Exception:
        video_count = 0

    return story_text, image_count, video_count


def crawl_creator_from_creator_page(driver):
    result = {
        "creator_name": "",
        "projects_backed_text": "",
        "number_of_projects": "",
        "creator_account_created_date": "",
        "creator_description": "",
    }

    def norm(s):
        return (s or "").strip()

    def lower_no_ws(s):
        return norm(s).lower()

    try:
        cont = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".grid-col-12.grid-col-8-md"))
        )
    except Exception as e:
        print("Creator main container not found:", e)
        return result

    try:
        blk = cont.find_element(By.CSS_SELECTOR, ".kds-flex.kds-items-center.kds-gap-05.kds-mb-06")
        col = blk.find_element(By.CSS_SELECTOR, ".kds-flex.kds-flex-col.kds-gap-02")
        name = norm(col.find_element(By.TAG_NAME, "h3").text)
        if name:
            result["creator_name"] = name
    except Exception:
        pass

    wrapper = None
    try:
        wrapper = cont.find_element(
            By.XPATH,
            ".//*[contains(@class,'kds-flex') and contains(@class,'kds-flex-row') and "
            "contains(@class,'kds-flex-wrap') and contains(@class,'kds-gap-04') and "
            "contains(@class,'md:kds-gap-16')]"
        )
    except Exception:
        pass

    if wrapper:
        try:
            inner = wrapper.find_element(
                By.XPATH,
                ".//*[contains(@class,'kds-flex') and contains(@class,'kds-flex-col-reverse') and "
                "contains(@class,'basis-auto-md') and contains(@class,'kds-mb-03') and "
                "contains(@class,'md:kds-mb-0') and contains(@class,'basis100p') and "
                "contains(@class,'do-not-visually-track')]"
            )
            a_links = inner.find_elements(By.XPATH, ".//a[contains(@class,'kds-text-primary')]")

            def a_text(ael, prefer_lg=False):
                if prefer_lg:
                    try:
                        sp = ael.find_element(By.XPATH, ".//span[contains(@class,'kds-type-heading-lg')]")
                        return norm(sp.text)
                    except Exception:
                        pass
                try:
                    sp = ael.find_element(By.XPATH, ".//span[contains(@class,'kds-type-heading-sm')]")
                    return norm(sp.text)
                except Exception:
                    pass
                return norm(ael.text)

            if len(a_links) >= 1:
                result["projects_backed_text"] = a_text(a_links[0], prefer_lg=False)
            if len(a_links) >= 2:
                result["number_of_projects"] = a_text(a_links[1], prefer_lg=True)
        except Exception:
            pass
        try:
            blocks = wrapper.find_elements(
                By.XPATH,
                ".//div[contains(@class,'kds-flex') and contains(@class,'kds-flex-col-reverse') and "
                "contains(@class,'do-not-visually-track')]"
            )
            for b in blocks:
                try:
                    label_sm = b.find_element(By.XPATH, ".//span[contains(@class,'kds-type-heading-sm')]")
                    lbl = lower_no_ws(label_sm.text)
                except Exception:
                    continue
                if any(k in lbl for k in ["cuenta creada", "account created", "member since",
                                           "joined", "miembro desde"]):
                    try:
                        val_lg = b.find_element(By.XPATH, ".//span[contains(@class,'kds-type-heading-lg')]")
                        result["creator_account_created_date"] = norm(val_lg.text)
                        break
                    except Exception:
                        pass
        except Exception:
            pass

    try:
        bio_el = driver.find_element(
            By.XPATH,
            "//*[contains(@class,'text-preline') and contains(@class,'do-not-visually-track') and "
            "contains(@class,'kds-type') and contains(@class,'kds-type-body-md')]"
        )
        bio_text = norm(bio_el.text)
        if bio_text:
            result["creator_description"] = bio_text
    except Exception:
        pass

    return result


def _find_rewards_sidebar(driver):
    try:
        return driver.find_element(By.ID, "react-rewards-sidebar")
    except Exception:
        pass
    for sel in ["[data-test-id='rewards-sidebar']", "[role='complementary']"]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            if el:
                return el
        except Exception:
            continue
    return None


def _click_nav_tab(driver, tab_text="Rewards"):
    try:
        navs = driver.find_elements(By.XPATH, "//nav|//header")
        for nav in navs:
            for b in nav.find_elements(By.XPATH, ".//a|.//button"):
                t = (b.text or b.get_attribute("outerText") or "").strip().lower()
                if tab_text.lower() in t:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", b)
                    jitter_short()
                    try:
                        b.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", b)
                    time.sleep(0.8)
                    return True
    except Exception:
        pass
    return False


def _parse_rewards_from(scope):
    rewards_data = []
    for idx, card in enumerate(scope.find_elements(By.TAG_NAME, "article"), 1):
        try:
            name = ""
            for xp in [".//header//*[self::h3 or self::h2 or self::h4]", ".//*[@data-test-id='reward-title']"]:
                els = card.find_elements(By.XPATH, xp)
                if els:
                    name = (els[0].text or "").strip()
                    if name:
                        break
            if not name:
                name = f"Reward #{idx}"
            try:
                price = card.find_element(By.XPATH, ".//header//p|.//*[@data-test-id='reward-amount']").text.strip()
            except Exception:
                price = ""
            desc = ""
            try:
                blk = card.find_element(
                    By.XPATH,
                    ".//*[contains(@class,'block') and contains(@class,'z1') or "
                    "contains(@class,'content') or @data-test-id='reward-description']"
                )
                desc = (blk.text or "").strip()
            except Exception:
                ps = card.find_elements(By.TAG_NAME, "p")
                desc = "\n".join((p.text or "").strip() for p in ps if (p.text or "").strip())
            try:
                ships_to = card.find_element(By.XPATH, ".//div[h3[contains(.,'Ships to')]]/div").text.strip()
            except Exception:
                ships_to = ""
            try:
                est = card.find_element(By.XPATH, ".//div[h3[contains(.,'Estimated delivery')]]//time").text.strip()
            except Exception:
                est = ""
            try:
                limited = card.find_element(By.XPATH, ".//div[h3[contains(.,'Limited quantity')]]/div").text.strip()
            except Exception:
                limited = ""
            rewards_data.append({
                "name": name,
                "price": price,
                "description": desc,
                "ships_to": ships_to,
                "estimated_delivery": est,
                "limited_quantity": limited,
            })
        except Exception:
            continue
    return rewards_data


def crawl_rewards(driver, state, project_url=None, headless=False, version_main=140):
    sidebar = _find_rewards_sidebar(driver)
    if sidebar:
        click_all_see_more(driver, sidebar)
        data = _parse_rewards_from(sidebar)
        if data:
            return data
    if project_url:
        driver, ok = open_or_rotate(driver, project_url, state, headless=headless, version_main=version_main)
        if not ok:
            return []
        sidebar = _find_rewards_sidebar(driver)
        if sidebar:
            click_all_see_more(driver, sidebar)
            data = _parse_rewards_from(sidebar)
            if data:
                return data
        if _click_nav_tab(driver, "Rewards"):
            time.sleep(0.8)
            try:
                main_rewards = driver.find_element(
                    By.XPATH, "//main|//div[@role='main']|//section[contains(@aria-label,'Rewards')]"
                )
                click_all_see_more(driver, main_rewards)
                data = _parse_rewards_from(main_rewards)
                if data:
                    return data
            except Exception:
                pass
    return []


def load_more_loop_with_budget(driver, scope, max_clicks=80, settle_timeout=7):
    def visible_items():
        its = scope.find_elements(By.XPATH, ".//article|.//li|.//*[@role='listitem']")
        return [i for i in its if i.is_displayed()]

    clicks = 0
    while clicks < max_clicks:
        ra, _u = pop_last_graph_retry_after(driver)
        if ra:
            print(f"[network] 429 during load-more, backoff {ra}s and rotate.")
            return False
        before = len(visible_items())
        candidates = []
        for xp in [".//button[normalize-space()]", ".//*[@role='button']"]:
            candidates += scope.find_elements(By.XPATH, xp)

        def match_text(el):
            try:
                t = (el.text or el.get_attribute("aria-label") or el.get_attribute("outerText") or "").strip().lower()
            except Exception:
                t = ""
            keys = ["load more", "show more", "cargar más", "mostrar más", "ver más"]
            return any(k in t for k in keys)

        btns = [b for b in candidates if b.is_displayed() and match_text(b)]
        if not btns:
            print("No more Load/Show buttons.")
            break
        btn = btns[0]
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        jitter_short()
        try:
            btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", btn)
        clicks += 1
        print(f"Load-more click #{clicks}")
        try:
            WebDriverWait(driver, settle_timeout).until(
                lambda d: len(visible_items()) > before or pop_last_graph_retry_after(driver)[0] is not None
            )
        except TimeoutException:
            if len(visible_items()) <= before:
                print("No new items after click. Assuming done.")
                break
        ra2, _u2 = pop_last_graph_retry_after(driver)
        if ra2:
            print(f"[network] 429 detected after click, backoff {ra2}s and rotate.")
            return False
        jitter_short()
    return True


def extract_updates(driver):
    try:
        try:
            project_post_interface = driver.find_element(By.ID, "project-post-interface")
            updates = project_post_interface.find_elements(By.CLASS_NAME, "truncated-post")
        except NoSuchElementException:
            updates = driver.find_elements(By.CSS_SELECTOR, "[data-test-id='post'], article.truncated-post")
            if not updates:
                updates = driver.find_elements(By.XPATH, "//article[.//h2 or .//h3]")

        try:
            update_count_wrapper = driver.find_element(By.ID, 'updates-emoji')
            update_count_result = update_count_wrapper.find_element(By.CLASS_NAME, 'count').text.strip()
        except Exception:
            update_count_result = str(len(updates))

        update_data_result = {}
        for i, update in enumerate(updates, 1):
            try:
                try:
                    update_number_element = update.find_element(By.XPATH, ".//span[contains(text(), 'Update #')]")
                    update_number = update_number_element.text.strip()
                except NoSuchElementException:
                    update_number = f"Update #{i}"
                title = ""
                for tag in ("h2", "h3"):
                    try:
                        title = update.find_element(By.TAG_NAME, tag).text.strip()
                        if title:
                            break
                    except NoSuchElementException:
                        continue
                try:
                    rte_content = update.find_element(By.CLASS_NAME, "rte__content")
                    content = rte_content.text.strip()
                except NoSuchElementException:
                    ps = update.find_elements(By.TAG_NAME, "p")
                    content = "\n".join(p.text.strip() for p in ps if p.text.strip()) or "No content found."
                update_data_result[update_number] = {"title": title, "content": content}
            except NoSuchElementException:
                print("Skipping update due to missing elements.")
        return update_count_result, update_data_result
    except Exception as e:
        print(f"Error extracting updates: {e}")
        return "0", {}


def extract_community_data(driver):
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CLASS_NAME, "community-section__hero"))
        )
        backers_text = driver.find_element(By.CSS_SELECTOR, ".community-section__hero .title").text.strip()
        m = re.search(r"\d[\d,\.]*", backers_text)
        backers_count = m.group(0) if m else "0"

        city_data = []
        for item in driver.find_elements(By.CSS_SELECTOR, ".community-section__locations_cities .location-list__item"):
            try:
                city = item.find_element(By.CSS_SELECTOR, ".primary-text").text.strip()
                country = item.find_element(By.CSS_SELECTOR, ".secondary-text").text.strip()
                backers = item.find_element(By.CSS_SELECTOR, ".tertiary-text").text.strip()
                city_data.append({f"{city}, {country}": backers})
            except Exception:
                continue

        country_data = []
        for item in driver.find_elements(By.CSS_SELECTOR, ".community-section__locations_countries .location-list__item"):
            try:
                country = item.find_element(By.CSS_SELECTOR, ".primary-text").text.strip()
                backers = item.find_element(By.CSS_SELECTOR, ".tertiary-text").text.strip()
                country_data.append({country: backers})
            except Exception:
                continue

        new_backers = 0
        returning_backers = 0
        try:
            new_backers = int(driver.find_element(By.CSS_SELECTOR, ".new-backers .count").text.strip())
        except Exception:
            pass
        try:
            returning_backers = int(driver.find_element(By.CSS_SELECTOR, ".existing-backers .count").text.strip())
        except Exception:
            pass

        return {
            "backers_count": backers_count,
            "where_backers_come_from_cities": city_data,
            "where_backers_come_from_countries": country_data,
            "new_backers": new_backers,
            "returning_backers": returning_backers,
        }
    except Exception as e:
        print(f"Error extracting community data: {e}")
        return {}


def crawl_faqs(driver):
    faqs = []
    try:
        ul = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "ul.faqs.grid-col-8-sm"))
        )
    except TimeoutException:
        print("FAQs list not found.")
        return faqs

    items = ul.find_elements(By.XPATH, "./li[contains(@class,'js-faq')]")
    print(f"Found {len(items)} faq items.")
    for li in items:
        try:
            question = ""
            try:
                a_toggle = li.find_element(By.XPATH, ".//a[contains(@class,'js-faq-question-toggle')]")
                try:
                    span_q = a_toggle.find_element(
                        By.XPATH, ".//span[contains(@class,'type-14') and contains(@class,'navy-700') and contains(@class,'medium')]"
                    )
                    question = (span_q.text or "").strip()
                except Exception:
                    question = (a_toggle.text or "").strip()
            except Exception:
                try:
                    span_q = li.find_element(
                        By.XPATH, ".//span[contains(@class,'type-14') and contains(@class,'navy-700') and contains(@class,'medium')]"
                    )
                    question = (span_q.text or "").strip()
                except Exception:
                    question = ""

            def find_answer_text():
                try:
                    ans_div = li.find_element(By.XPATH, ".//div[contains(@class,'js-faq-answer')]")
                    inner = ans_div.find_element(
                        By.XPATH, ".//*[contains(@class,'type-14') and contains(@class,'navy-700') and contains(@class,'normal')]"
                    )
                    ps = inner.find_elements(By.TAG_NAME, "p")
                    if ps:
                        return "\n\n".join([(p.text or "").strip() for p in ps if (p.text or "").strip()])
                    return (inner.text or "").strip()
                except Exception:
                    return ""

            answer = find_answer_text()
            if not answer:
                try:
                    a_toggle = li.find_element(By.XPATH, ".//a[contains(@class,'js-faq-question-toggle')]")
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", a_toggle)
                    jitter_short()
                    try:
                        a_toggle.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", a_toggle)
                    time.sleep(0.35)
                    answer = find_answer_text()
                except Exception:
                    pass
            if question or answer:
                faqs.append({"question": question, "answer": answer})
        except Exception as e:
            print("Error parsing one FAQ:", e)
            continue
    return faqs


def load_all_comments(driver, max_clicks=120):
    try:
        container = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, "react-project-comments"))
        )
    except TimeoutException:
        print("No comments container found.")
        return True
    return load_more_loop_with_budget(driver, container, max_clicks=max_clicks, settle_timeout=7)


def extract_comments_data(driver):
    container = WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.ID, "react-project-comments"))
    )
    try:
        root = container.find_element(By.TAG_NAME, "ul")
        items = root.find_elements(By.XPATH, "./li")
    except Exception:
        items = container.find_elements(By.XPATH, ".//*[@role='listitem']")
    items = [i for i in items if i.is_displayed()]
    comments_count = len(items)
    print(f"Found {comments_count} comments.")

    def extract_author(scope):
        for xp in [
            ".//span[contains(@class,'do-not-visually-track')]",
            ".//a[contains(@href,'/profile/')]",
            ".//span[@data-test-id='author-name']",
            ".//strong",
        ]:
            els = scope.find_elements(By.XPATH, xp)
            if els and els[0].text.strip():
                return els[0].text.strip()
        return ""

    comments_data = []
    for item in items:
        try:
            username = extract_author(item)
            try:
                t_el = item.find_element(By.TAG_NAME, "time")
                datetime_val = t_el.get_attribute("title") or t_el.get_attribute("datetime") or t_el.text.strip()
                if not datetime_val:
                    datetime_val = "Unknown"
            except Exception:
                datetime_val = "Unknown"

            content = ""
            for xp in [
                ".//p[contains(@class,'data-comment-text')]",
                ".//div[contains(@class,'rte__content')]//p",
                ".//p",
            ]:
                ps = item.find_elements(By.XPATH, xp)
                if ps:
                    content = "\n".join([p.text.strip() for p in ps if p.text.strip()])
                    if content:
                        break

            replies = []
            is_creator_reply = False
            for reply in item.find_elements(By.XPATH, ".//ul//li|.//*[@role='listitem']"):
                if not reply.is_displayed():
                    continue
                try:
                    r_user = extract_author(reply)
                    try:
                        t_el = reply.find_element(By.TAG_NAME, "time")
                        r_time = t_el.get_attribute("title") or t_el.get_attribute("datetime") or t_el.text.strip()
                    except Exception:
                        r_time = "Unknown"
                    r_text_nodes = reply.find_elements(By.XPATH, ".//p[contains(@class,'data-comment-text')]|.//p")
                    r_text = "\n".join([p.text.strip() for p in r_text_nodes if p.text.strip()])
                    if reply.find_elements(By.XPATH, './/span[contains(text(), "Creator")]'):
                        is_creator_reply = True
                    replies.append({"username": r_user or "Unknown", "datetime": r_time, "content": r_text})
                except Exception:
                    continue

            comments_data.append({
                "username": username or "Unknown",
                "datetime": datetime_val,
                "content": content,
                "replies": replies,
                "replies_count": len(replies),
                "is_creator_reply": is_creator_reply,
            })
        except Exception as e:
            print(f"Error extracting comment: {e}")
    return {"comments_count": comments_count, "comments": comments_data}
