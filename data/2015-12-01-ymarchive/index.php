<?
  include "mysql.php";
  include "style.php";
  function make_color($r, $g=-1, $b=-1) {
    if (is_array($r) && sizeof($r) == 3)
        list($r, $g, $b) = $r;

    $r = intval($r); 
    $g = intval($g);
    $b = intval($b);

    $r = dechex($r<0?0:($r>255?255:$r));
    $g = dechex($g<0?0:($g>255?255:$g));
    $b = dechex($b<0?0:($b>255?255:$b));

    $color = (strlen($r) < 2?'0':'').$r;
    $color .= (strlen($g) < 2?'0':'').$g;
    $color .= (strlen($b) < 2?'0':'').$b;
    return '#'.$color;
  }
  
  function show_temp(&$temp, &$damp) {
    GLOBAL $av_n;
    $max=20;
    echo "<td><td width=12 bgcolor=".make_color(($temp+$max)*255/2/$max, 0, 255-($temp+$max)*255/2/$max)."><font color=white><b><center>".round($temp);
    $max=20;
    echo "<td width=12 bgcolor=".make_color(255-($damp/10)*255/$max, 255-($damp/10)*255/$max, 255).">";
    if ($damp>0.4) echo "<b><center>".round($damp/10);
    $temp = 0;
    $damp = 0;
    $av_n = 0;
  }

  $m = $_GET[m];
  $d = $_GET[d];
  //$hour = $_GET[hour];
  
  if ($m == "") $m = date("m");
  if ($d == "") $d = date("d");
  //if ($hour == "") $hour = date("H");
  
  function ln($y, $m, $d, $h) {
    $m = str_pad($m, 2, "0", STR_PAD_LEFT);
    $d = str_pad($d, 2, "0", STR_PAD_LEFT);
    $h = str_pad($h, 2, "0", STR_PAD_LEFT);
    return "result/$y-$m/$y-$m-$d-$h.png";
  }
  
  echo "<center>";
  echo "<form action=index.php method=get>";
  echo "<title>Архив Яндекс пробок</title>";
  echo "<h2><center>Архив Яндекс пробок</center></h2>";
  echo "<b>Введите дату для путешествия в прошлое</b><p>";
  echo "Месяц: <input style='width:50px;' type=number name=m value='$m'> ";
  echo "День: <input style='width:50px;' type=number name=d value='$d'> ";
  //echo "Час: <input size=5 type=text name=hour value='$hour'> ";
  echo "<input type=submit> <a href=index.php>Сегодня</a> | ";
  $cur_year = date("Y");
  $next_day = strtotime("$cur_year-$m-$d +1 day");
  $prev_day = strtotime("$cur_year-$m-$d -1 day");
  echo "<a href=index.php?m=".date("m", $prev_day)."&d=".date("d", $prev_day).">Назад</a> ";
  echo "<a href=index.php?m=".date("m", $next_day)."&d=".date("d", $next_day).">Вперед</a> ";
  echo "</form>";
  
  $m = str_pad($m, 2, "0", STR_PAD_LEFT);
  $d = str_pad($d, 2, "0", STR_PAD_LEFT);
  //$hour = str_pad($hour, 2, "0", STR_PAD_LEFT);
  
  echo "<table border=1 cellpadding=3 style='border-collapse: collapse;'>";
  $d_int = 4;
  $h_int = 5;
  $old_h=-1;
  for ($i=$cur_year; $i>2012; $i--) {
    //echo "result/$i-$m/$i-$m-$d-$hour.png";
    //if (!file_exists(ln($i, $m, $d, 0))) continue;
    $q = "SELECT *,DATE(hour) as date, MONTH(hour) as m, DAYOFWEEK(hour) as dow,
      DAY(hour) as d, HOUR(hour) as h  FROM hours 
      WHERE hour >= DATE_ADD('$i-$m-$d', INTERVAL -$d_int DAY) 
      AND hour < DATE_ADD('$i-$m-$d', INTERVAL ".($d_int+1)." DAY) 
      ORDER BY hour
    ";
    //AND HOUR(hour)>=$hour-$h_int 
    //AND HOUR(hour)<=$hour+$h_int
    $r = mysql_query($q);
    echo mysql_error();
    $n = mysql_numrows($r);
    if ($n == 0) continue;
    $old_date = "";
    if ($old_h>0) while ($old_h<23) {
      $old_h++;
      echo "<td>";
    }
    if ($x>0) show_temp($av_temp, $av_damp);
    echo "<tr><th colspan=25><a target=_blank href='http://www.consultant.ru/law/ref/calendar/proizvodstvennye/$i/'>$i</a> год";
    echo "<td><th title='Температура (средня за сутки)'>T<th title='Осадки, мм (суммарно за сутки)'>О";
    $old_h=-1;
    for ($x=0; $x<$n; $x++) {
      $w = mysql_fetch_assoc($r);
      $av_temp = ($av_temp*$av_n+$w[temp])/($av_n+1);
      $av_damp += $w[damp];
      $av_n++;
      if ($w[date] != $old_date) {
        if ($old_h>0) while ($old_h<23) {
          $old_h++;
          echo "<td>";
        }
        $old_h=-1;
        $font="<font color=black>";
        if (($w[dow] == 7) || ($w[dow] == 1)) $font="<font color=red>";
        if ($x>0) show_temp($av_temp, $av_damp);
        echo "<tr><th><a href=?m=$w[m]&d=$w[d]>$font$w[d]/$w[m]";
        $style="";
        if ($w[d] != $d) $style = "style='font-weight: normal'";
      }
      while ($old_h<($w[h]-1)) {
        $old_h++;
        echo "<td>";
      }
      $old_h=$w[h];
      $color="white";
      //if ($w[traf]+abs($w[temp])+$w[damp] != 0) {
        if ($w[traf] == 0) $color="#00ff00";
        if ($w[traf] == 1) $color="#22ee00";
        if ($w[traf] == 2) $color="#44dd00";
        if ($w[traf] == 3) $color="#66cc00";
        if ($w[traf] == 4) $color="#88bb00";
        if ($w[traf] == 5) $color="#aaaa00";
        if ($w[traf] == 6) $color="#bb8800";
        if ($w[traf] == 7) $color="#cc6600";
        if ($w[traf] == 8) $color="#dd4400";
        if ($w[traf] == 9) $color="#ee2200";
        if ($w[traf] ==10) $color="#ff0000";
      //}
      echo "<td bgcolor=$color><center><a href='".ln($i, $w[m], $w[d], $w[h])."'><font $style color=black>";
      if ($w[damp]>0) echo "<u>";
      echo "$w[h]";
      $old_date = $w[date];
    }
  }
  if ($old_h>0) while ($old_h<23) {
    $old_h++;
    echo "<td>";
  }
  show_temp($av_temp, $av_damp);
  echo "</table>";
  echo "<div style='max-width: 500px'>";
  echo "<p>В ячейках таблицы указан час дня. Красные даты - выходные. Подчеркнуты часы c дождем или снегом.";
  echo "<p>Выберите наилучший день для поездки, учитывая статистику предыдущих лет. Лучше выехать на дачу утром <a href=?m=5&d=1>1 мая</a> или <a href=?m=5&d=9>9 мая</a>? Когда лучше вернуться с дачи, 3 или <a href=?m=5&d=5>5 мая</a>? Когда лучше поехать в магазин за подарком, 29, 30 или <a href=?m=12&d=31>31 декабря</a>? В икею лучше поехать 1, 2 или <a href=?m=1&d=9>9 января</a>? Стоит ли ехать за город <a href=?m=6&d=12>12 июня</a>, <a href=?m=3&d=8>8 марта</a>, <a href=?m=2&d=23>23 февраля</a>, <a href=?m=11&d=4>4 ноября</a>?";
  echo "<p>Не забудьте проверить <a target=_blank href='https://www.gismeteo.ru/city/weekly/4368/'>погоду</a> на запланированный день, а в день поездки проверьте <a href=http://www.rbc.ru/>новости</a> и используйте <a target=_blank href='https://maps.yandex.ru/'>навигатор Яндекс</a> для того, чтобы не попасть в неожиданную пробку, связанную с перекрытием дорог, ДТП или ремонтом.";
  echo "</div>";
?>