" see :options
" Long json can throw an error; increase pattern matching memory
set maxmempattern=50000
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
hi clear SpellBad
hi SpellBad ctermfg=7 ctermbg=1
" hi clear SpellRare
" hi SpellRare cterm=underline
" hi clear SpellCap
" hi SpellCap cterm=underline
hi clear SpellLocal
hi SpellLocal cterm=underline
" Set some file types based on extension
autocmd BufNewFile,BufRead *.json set filetype=json
autocmd BufNewFile,BufRead *.pac set filetype=javascript
autocmd BufNewFile,BufRead *.styl set filetype=stylus
" turn on spell checking for some file extensions
autocmd BufNewFile,BufRead *.md setlocal spell spelllang=en_us

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

" adjust colors so in 256color mode I can still see things
" type "highlight" to see what the current settings are
hi Search term=reverse ctermfg=0 ctermbg=3 guibg=Yellow
hi clear Visual
hi Visual term=reverse cterm=reverse guibg=LightGrey
hi DiffAdd term=bold ctermbg=4 guibg=LightBlue
hi DiffChange term=bold ctermbg=5 guibg=LightMagenta
hi DiffText term=reverse cterm=bold ctermbg=1 gui=bold guibg=Red
hi SpellBad ctermfg=7 ctermbg=1
hi SpellCap term=reverse ctermbg=4 gui=undercurl guisp=Blue
hi SpellRare term=reverse ctermbg=5 gui=undercurl guisp=Magenta
hi SpellLocal cterm=underline
hi clear CursorColumn
hi CursorColumn term=reverse cterm=reverse gui=reverse
hi ColorColumn term=reverse ctermbg=1 guibg=LightRed
hi QuickFixLine term=reverse ctermfg=0 ctermbg=3 guibg=Yellow
hi MatchParen term=reverse ctermfg=0 ctermbg=3 guibg=Yellow
hi ToolbarLine term=underline ctermfg=0 ctermbg=3 guibg=Yellow

" syntastic block start
if 1
"syntastic requires pathogen (see https://github.com/scrooloose/syntastic)
silent! call pathogen#infect()

let g:syntastic_always_populate_loc_list = 1
let g:syntastic_auto_loc_list = 1
let g:syntastic_check_on_open = 0
let g:syntastic_check_on_wq = 0
let g:syntastic_javascript_checkers=['eslint']
let g:syntastic_pug_checkers=['pug_list']
"let g:syntastic_javascript_eslint_exe = '$(npm bin)/eslint'
let g:syntastic_stylus_checkers=['stylint']
let g:syntastic_python_python_exec = 'python3.7'
let g:syntastic_python_flake8_exe = 'python3.7 -m flake8'
let g:syntastic_python_flake8_args = '--format=''%(path)s:%(row)d:%(col)d: %(code)s %(text)s'''

let g:SuperTabNoCompleteAfter=['^', '\s', '\*', '//']
endif
" syntastic block end


" remove trailing whitespace
function! s:StripTrailingWhitespaces()
    let l = line(".")
    let c = col(".")
    try
        silent undojoin
    catch
    endtry
    %s/\s\+$//e
    call cursor(l, c)
endfunction

" remove empty lines at the end of a file
function! s:TrimEndLines()
    let s:l = line(".")
    let s:c = col(".")
    try
        silent undojoin
    catch
    endtry
    %s#\($\n\s*\)*\%$##
    call cursor(s:l, s:c)
endfunction

function! s:AddEndLine()
    let l = line(".")
    let c = col(".")
    let m = &modified
    try
        silent undojoin
    catch
    endtry
    $s#$#\r#
    let &modified = m
    call cursor(l, c)
endfunction

function! s:AddEndLineAfterWrite()
    call s:AddEndLine()
    call cursor(s:l, s:c)
endfunction

autocmd! BufNewFile,BufRead *.vs,*.fs set ft=glsl
autocmd FileType c,cpp,javascript,jade,php,ruby,python,stylus,pug,cmake,yaml,tmpl,dockerfile,vue,glsl,dosini,typescript autocmd BufWritePre <buffer> :call s:StripTrailingWhitespaces()
autocmd FileType c,cpp,javascript,jade,php,ruby,python,stylus,pug,cmake,yaml,tmpl,dockerfile,vue,glsl,dosini,typescript autocmd BufWritePre <buffer> :call s:TrimEndLines()
autocmd FileType c,cpp,javascript,jade,php,ruby,python,stylus,pug,cmake,yaml,tmpl,dockerfile,vue,glsl,dosini,typescript autocmd BufEnter <buffer> :call s:AddEndLine()
autocmd FileType c,cpp,javascript,jade,php,ruby,python,stylus,pug,cmake,yaml,tmpl,dockerfile,vue,glsl,dosini,typescript autocmd BufWritePost <buffer> :call s:AddEndLineAfterWrite()

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
set synmaxcol=1000
" Sometime syntax doesn't search far enough to format properly.  'fromstart' is
" slow, but will work.  'minlines=..' is less slow.  'clear' resets to the
" defaults.  This doesn't stick for many file types
" syntax sync fromstart
" syntax sync minlines=200
autocmd FileType vue syntax sync fromstart
" don't do syntax highlight on big files
autocmd BufReadPre * if getfsize(expand("%")) > 10000000 | syntax clear | endif

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

" vim-plug block start
if 0
" Install vim-plug
" curl -fLo "${XDG_DATA_HOME:-$HOME/.local/share}/nvim/site/autoload/plug.vim" --create-dirs https://raw.githubusercontent.com/junegunn/vim-plug/master/plug.vim
call plug#begin('~/.vim/plugged')
" completion plugins
Plug 'https://github.com/Valloric/YouCompleteMe'
" linting plugins
Plug 'dense-analysis/ale'
call plug#end()
" run :PlugInstall once to install plugins

" Autoclose completion window
" autocmd InsertLeave,CompleteDone * if pumvisible() == 0 | pclose | endif
autocmd CompleteDone * if pumvisible() == 0 | pclose | endif

" linting options
let g:ale_sign_column_always = 0
" Insert Mode -> normal cursor (line)
let &t_SI .= "\e[5 q"
" " Normal Mode -> block cursor
let &t_EI .= "\e[1 q"
endif
" vim-plug block end


" fix gitcommit 
autocmd FileType gitcommit setlocal spell spelllang=en_us
" On git commits, reflow paragraphs and set the text width to 72.
autocmd FileType gitcommit set tw=72
" autocmd FileType gitcommit set formatoptions+=at
autocmd FileType gitcommit set formatoptions=cqat
" But this looks bad in github PRs, so stop doing it.
" autocmd FileType gitcommit set formatoptions-=at


