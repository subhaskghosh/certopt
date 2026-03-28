SELECT
  x.firstname,
  x.lastname,
  y.city,
  y.state
FROM PERSON AS X
LEFT JOIN ADDRESS AS Y
  ON x.personid = y.personid