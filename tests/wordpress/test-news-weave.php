<?php
// Run via PHP stdin with a read-only project mount; no live WordPress is used.
define('ABSPATH', '/project/');
$callbacks = [];
$registered = [];
$activation = null;
$flush_count = 0;
function add_action($hook, $callback) {
    global $callbacks;
    $callbacks[$hook] = $callback;
}
function register_activation_hook($file, $callback) {
    global $activation;
    $activation = $callback;
}
function register_post_type($name, $options) {
    global $registered;
    $registered = [$name, $options];
}
function flush_rewrite_rules() {
    global $flush_count;
    $flush_count++;
}
function check($condition, $message) {
    if (!$condition) {
        throw new RuntimeException($message);
    }
}
require '/project/wordpress/news-weave/news-weave.php';
check(isset($callbacks['init']), 'Missing init callback');
call_user_func($callbacks['init']);
check($registered[0] === 'news_weave', 'Post type mismatch');
$options = $registered[1];
check($options['labels']['name'] === 'ニュースを編む', 'Display name mismatch');
check($options['labels']['singular_name'] === 'ニュースを編む', 'Singular name mismatch');
check($options['rest_base'] === 'news-weave', 'REST route mismatch');
check($options['rewrite']['slug'] === 'news-weave', 'URL mismatch');
check($options['show_in_rest'] === true, 'REST disabled');
check(in_array('thumbnail', $options['supports'], true), 'Featured images unsupported');
check($options['taxonomies'] === ['category', 'post_tag'], 'Taxonomies mismatch');
check($flush_count === 0, 'Rewrite flushed during init');
call_user_func($activation);
check($flush_count === 1, 'Activation must flush rewrite rules');
echo "PASS: current identities, URLs and labels\n";
