" see :options
" expandtabs
set et
set tabstop=4
" shiftwidth
set sw=4
set nocindent
" autoindent
set ai
" tell indenting programs that we already indented the buffer
let b:did_indent = 1
" don't do an incremental search (don't search before we finish typing)
set nois
" don't ignore case by default
set noic
" don't break at 80 characters
set wrap
" don't add linebreaks at 80 characters
set nolbr
" highlight all search matches
set hls
" make Spell turn on spell checking
command Spell set spell spelllang=en_us
" Set some file types based on extension
autocmd BufNewFile,BufRead *.json set filetype=json
autocmd BufNewFile,BufRead *.pac set filetype=javascript
autocmd BufNewFile,BufRead *.styl set filetype=stylus
" turn on spell checking for some file extensions
autocmd BufNewFile,BufRead *.md setlocal spell spelllang=en_us
" and for some file types
autocmd FileType gitcommit setlocal spell spelllang=en_us
" type zg when over a 'misspelled' word to add it to the spellfile dictionary
"      z= to show spelling suggestions.
" default to utf-8
set enc=utf-8
" show the cursor position
set ruler
" allow backspace to go to the previous line
set bs=2
" keep this much history
set history=50
" don't try to maintain vi compatibility
set nocompatible

"syntastic requires pathogen (see https://github.com/scrooloose/syntastic)
silent! call pathogen#infect()

let g:syntastic_always_populate_loc_list = 1
let g:syntastic_auto_loc_list = 1
let g:syntastic_check_on_open = 0
let g:syntastic_check_on_wq = 0
let g:syntastic_javascript_checkers=['eslint']

" remove trailing whitespace
function! s:StripTrailingWhitespaces()
    let l = line(".")
    let c = col(".")
    %s/\s\+$//e
    call cursor(l, c)
endfunction

" remove empty lines at the end of a file
function! s:TrimEndLines()
    let s:l = line(".")
    let s:c = col(".")
    %s#\($\n\s*\)*\%$##
    call cursor(s:l, s:c)
endfunction

function! s:AddEndLine()
    let l = line(".")
    let c = col(".")
    let m = &modified
    $s#$#\r#
    let &modified = m
    call cursor(l, c)
endfunction

function! s:AddEndLineAfterWrite()
    call s:AddEndLine()
    call cursor(s:l, s:c)
endfunction

autocmd FileType c,cpp,javascript,jade,php,ruby,python,yaml,stylus,pug autocmd BufWritePre <buffer> :call s:StripTrailingWhitespaces()
autocmd FileType c,cpp,javascript,jade,php,ruby,python,yaml,stylus,pug autocmd BufWritePre <buffer> :call s:TrimEndLines()
autocmd FileType c,cpp,javascript,jade,php,ruby,python,yaml,stylus,pug autocmd BufEnter <buffer> :call s:AddEndLine()
autocmd FileType c,cpp,javascript,jade,php,ruby,python,yaml,stylus,pug autocmd BufWritePost <buffer> :call s:AddEndLineAfterWrite()

" backup to a single hidden directory with date-stamped backups.  Keep a
" maximum of 2500 files in the backup directory
set backupdir=$HOME/.vim_backup
if strlen(finddir(&g:backupdir))==0
    call mkdir(&g:backupdir, "p", 0770)
endif
if has("win32")
    call system("for /f \"tokens=* skip=2500\" \%F in ('dir ".shellescape(&g:backupdir)." /o-d /tc /b') do del ".shellescape(&g:backupdir."\\\%F"))
else
    call system("find ".shellescape(&g:backupdir)." -type f -print0 | xargs -0 ls -A1tr | head -n -2500 | xargs -d '\n' rm -f")
endif
execute "set backupext=_".strftime("%y%m%d%H%M")
set nobackup

function! s:save_copy(filename, ismod)
    " File must exist and be modified since creation
    let filename = fnamemodify(a:filename, ":p")
    if !filereadable(filename)
        return
    endif
    if (!a:ismod)
        return
    endif
    " Don't backup files that are more than 10 Mb
    if (getfsize(filename) > 10000000)
        return
    endif
    let backup=fnamemodify(&backupdir, ":p").fnamemodify(filename, ":t")."_".strftime("%y%m%d%H%M%S", getftime(filename))
    if has("win32")
        let cmd = "copy /y ".shellescape(filename)." ".shellescape(backup)
    else
        let cmd = "cp ".fnameescape(filename)." ".fnameescape(backup)
    endif
    let result = system(cmd)
endfunction

autocmd BufWritePre * let ismod=&mod
autocmd BufWritePre,BufWritePost * call s:save_copy(expand('<afile>'), ismod)
" end of backup settings


" syntax highlighting is on
syntax on
" save information for 100 files, with up to 50 lines for each register
set viminfo='100,\"50
if v:lang =~ "utf8$" || v:lang =~ "UTF-8$"
    set fileencodings=utf-8,latin1
endif
if has("autocmd")
    " When editing a file, always jump to the last cursor position
    autocmd BufReadPost *
    \ if line("'\"") > 0 && line ("'\"") <= line("$") |
    \   exe "normal! g'\"" |
    \ endif
endif
autocmd FileType c set nocindent
