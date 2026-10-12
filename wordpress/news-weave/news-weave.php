<?php
/**
 * Plugin Name: News Weave
 * Description: 「ニュースを編む」のREST API対応ニュース投稿タイプ。
 * Version: 2.0.0
 * Text Domain: news-weave
 */

if (!defined('ABSPATH')) {
    exit;
}

function news_weave_register_news_type() {
    register_post_type('news_weave', [
        'labels' => [
            'name' => 'ニュースを編む',
            'singular_name' => 'ニュースを編む',
        ],
        'public' => true,
        'show_in_rest' => true,
        'rest_base' => 'news-weave',
        'has_archive' => false,
        'rewrite' => ['slug' => 'news-weave'],
        'menu_icon' => 'dashicons-media-document',
        'supports' => ['title', 'editor', 'excerpt', 'thumbnail', 'author', 'revisions'],
        'taxonomies' => ['category', 'post_tag'],
    ]);
}
add_action('init', 'news_weave_register_news_type');

function news_weave_activate_news_type() {
    news_weave_register_news_type();
    flush_rewrite_rules();
}
register_activation_hook(__FILE__, 'news_weave_activate_news_type');



/** Adapt News Weave articles to the available viewport in the Cocoon theme. */
function news_weave_enqueue_article_layout() {
    if (!is_singular('news_weave')) {
        return;
    }
    wp_register_style('news-weave-layout', false, [], '2.0.0');
    wp_enqueue_style('news-weave-layout');
    wp_add_inline_style('news-weave-layout', <<<'CSS'
body.single-news_weave .wrap {
    width: calc(100% - clamp(16px, 3vw, 64px));
    max-width: none;
    box-sizing: border-box;
}
body.single-news_weave #content-in {
    display: grid;
    grid-template-columns: minmax(0, 1fr) clamp(240px, 21vw, 352px);
    gap: clamp(16px, 2vw, 32px);
    align-items: start;
}
body.single-news_weave #main,
body.single-news_weave #sidebar {
    width: 100%;
    min-width: 0;
    max-width: none;
    margin: 0;
    box-sizing: border-box;
}
body.single-news_weave #main {
    padding: clamp(16px, 2vw, 32px);
}
body.single-news_weave .entry-content {
    min-width: 0;
    overflow-wrap: anywhere;
}
body.single-news_weave .scrollable-table,
body.single-news_weave .table-scroll {
    max-width: 100%;
    overflow-x: auto;
    overscroll-behavior-x: contain;
}
body.single-news_weave .entry-content table {
    width: 100%;
    table-layout: fixed;
}
body.single-news_weave .entry-content table th,
body.single-news_weave .entry-content table td {
    white-space: normal;
    overflow-wrap: anywhere;
    word-break: normal;
    vertical-align: top;
    padding: clamp(8px, 1vw, 16px);
}
body.single-news_weave .entry-content table th:first-child {
    width: 18%;
}
@media (max-width: 1024px) {
    body.single-news_weave #content-in {
        grid-template-columns: minmax(0, 1fr);
    }
    body.single-news_weave #sidebar {
        grid-row: 2;
    }
}
@media (max-width: 600px) {
    body.single-news_weave .entry-content table {
        min-width: 640px;
        font-size: 15px;
    }
}
CSS
    );
}
add_action('wp_enqueue_scripts', 'news_weave_enqueue_article_layout', 100);
